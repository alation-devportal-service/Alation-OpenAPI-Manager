import streamlit as st
import yaml
import json
import subprocess
import shutil
import requests
import os
import urllib.parse
import tarfile
import re
import base64
from pathlib import Path

# ---------------------------------------------------------------------------
# PAGE CONFIG
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Alation OpenAPI Manager", page_icon="📘", layout="wide")

# ---------------------------------------------------------------------------
# GITHUB HELPERS
# ---------------------------------------------------------------------------

def gh_get(url, token, params=None):
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    return requests.get(url, headers=headers, params=params)

def gh_put(url, token, payload):
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    return requests.put(url, headers=headers, json=payload)

def load_slug_mapping(repo_name, token):
    """Fetches slug_mapping.json from the app's GitHub repo."""
    url = f"https://api.github.com/repos/{repo_name}/contents/slug_mapping.json"
    resp = gh_get(url, token)
    if resp.status_code == 200:
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return json.loads(content), data["sha"]
    elif resp.status_code == 404:
        return {}, None
    st.error(f"⚠️ Failed to load slug mapping: {resp.text}")
    return {}, None

def save_slug_mapping(repo_name, token, updated_mapping, sha):
    """Commits updated slug_mapping.json back to the app's GitHub repo."""
    url = f"https://api.github.com/repos/{repo_name}/contents/slug_mapping.json"
    encoded = base64.b64encode(
        json.dumps(updated_mapping, indent=4).encode("utf-8")
    ).decode("utf-8")
    payload = {
        "message": "🤖 Auto-update: Added new API slug mapping",
        "content": encoded,
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    resp = gh_put(url, token, payload)
    return resp.status_code in [200, 201]

def commit_file_to_branch(repo, token, branch, file_path, content_bytes, message):
    """
    Creates or updates a file on a GitHub branch.
    Returns True on success, False on failure.
    """
    url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
    existing = gh_get(url, token, params={"ref": branch})
    sha = existing.json().get("sha") if existing.status_code == 200 else None

    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    resp = gh_put(url, token, payload)
    return resp.status_code in [200, 201], resp

# ---------------------------------------------------------------------------
# NODE.JS SETUP
# ---------------------------------------------------------------------------

def ensure_node_installed():
    node_version = "v20.17.0"
    install_dir = Path("./node_runtime")
    node_dirname = f"node-{node_version}-linux-x64"
    node_bin_path = install_dir / node_dirname / "bin"

    try:
        if subprocess.run(["node", "-v"], capture_output=True).returncode == 0:
            return
    except FileNotFoundError:
        pass

    if not node_bin_path.exists():
        with st.spinner("🔧 Initializing environment (Node.js)..."):
            url = f"https://nodejs.org/dist/{node_version}/{node_dirname}.tar.xz"
            resp = requests.get(url, stream=True)
            tar_path = Path("node.tar.xz")
            with open(tar_path, "wb") as f:
                f.write(resp.raw.read())
            with tarfile.open(tar_path) as tar:
                tar.extractall(install_dir)
            os.remove(tar_path)

    os.environ["PATH"] = f"{str(node_bin_path.absolute())}{os.pathsep}{os.environ['PATH']}"

# ---------------------------------------------------------------------------
# COMMAND RUNNER
# ---------------------------------------------------------------------------

def run_command_ui(cmd_string, cwd=None, mask_secrets=[]):
    display_cmd = cmd_string
    for s in mask_secrets:
        if s:
            display_cmd = display_cmd.replace(s, "***")
    st.write(f"*> Running: {display_cmd}*")

    run_env = os.environ.copy()
    run_env["CI"] = "true"

    process = subprocess.Popen(
        cmd_string,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
        env=run_env,
    )
    for line in process.stdout:
        clean_line = line.strip()
        for s in mask_secrets:
            if s:
                clean_line = clean_line.replace(s, "***")
        st.text(clean_line)
    process.wait()
    return process.returncode

# ---------------------------------------------------------------------------
# OPENAPI FILE PREP (for ReadMe upload)
# ---------------------------------------------------------------------------

def prep_openapi_file(filepath, version, target_slug):
    with open(filepath, "r") as f:
        data = yaml.safe_load(f)
    if "info" not in data:
        data["info"] = {}
    data["info"]["version"] = version

    if "x-readme" not in data:
        data["x-readme"] = {}
    data["x-readme"]["explorer-enabled"] = False
    data["x-readme"]["proxy-enabled"] = True

    if "servers" in data and isinstance(data["servers"], list):
        for server in data["servers"]:
            if "variables" in server:
                if "protocol" in server["variables"]:
                    server["variables"]["protocol"]["default"] = "https"
                if "base-url" in server["variables"]:
                    server["variables"]["base-url"]["default"] = "alation_domain"

    yaml_filename = f"{target_slug}_prepped.yaml"
    yaml_filepath = filepath.parent / yaml_filename
    with open(yaml_filepath, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    return yaml_filepath

# ---------------------------------------------------------------------------
# MINTLIFY CONSTANTS
# ---------------------------------------------------------------------------

MINTLIFY_BRANCH   = "elena/testNavigationChanges"
DOCS_JSON_PATH    = "mintlify-poc-docs/docs.json"
API_REF_BASE      = "mintlify-poc-docs/api-reference"

VERSION_MAP = {
    "v2024.1.5":    "2024.1.5.0",
    "v2024.1.31":   "2024.1.31.0",
    "v2024.3":      "2024.3.0.0",
    "v2024.3.1-ja": "2024.3.1-ja",
    "v2024.3.1":    "2024.3.1.0",
    "v2024.3.2":    "2024.3.2.0",
    "v2024.3.4":    "2024.3.4.0",
    "v2024.3.5":    "2024.3.5.0",
    "v2025.1":      "2025.1.0.0",
    "v2025.1.2":    "2025.1.2.0",
    "v2025.1.3":    "2025.1.3.0",
    "v2025.1.4":    "2025.1.4.0",
    "v2025.1.5":    "2025.1.5.0",
    "v2025.3":      "2025.3.0.0",
    "v2025.3.1":    "2025.3.1.0",
    "v2025.3.2":    "2025.3.2.0",
    "v2025.3.3":    "2025.3.3.0",
    "v2025.3.4":    "2025.3.4.0",
    "v2026.1.0":    "2026.1.0.0",
    "v2026.2.0":    "2026.2.0.0",
    "v2026.2.1-0":  "2026.2.1.0",
    "v2026.3.1-0":  "2026.3.1.0",
    "v2026.5.0-0":  "2026.5.0.0",
}

# ---------------------------------------------------------------------------
# MAIN APP
# ---------------------------------------------------------------------------

def main():
    ensure_node_installed()
    st.title("📘 Alation OpenAPI Manager")

    # --- Secrets ---
    readme_key    = st.secrets.get("README_API_KEY", "")
    git_token     = st.secrets.get("GIT_TOKEN", "")
    git_user      = st.secrets.get("GIT_USER", "")
    eng_repo_url  = st.secrets.get("ENG_REPO_URL", "")
    path_main     = st.secrets.get("PATH_SPECS_MAIN", "django/static/swagger/specs")
    path_logical  = st.secrets.get("PATH_SPECS_LOGICAL", "django/static/swagger/specs/logical_metadata")
    svc_git_token = st.secrets.get("SVC_GIT_TOKEN", "")
    app_repo_name = st.secrets.get("APP_REPO_NAME", "")
    mintlify_repo = st.secrets.get("MINTLIFY_REPO_NAME", "")

    workspace_dir = Path("./temp_eng_workspace")
    workspace_dir.mkdir(exist_ok=True)

    # --- Load slug mapping ---
    current_mapping, current_sha = {}, None
    if svc_git_token and app_repo_name:
        current_mapping, current_sha = load_slug_mapping(app_repo_name, svc_git_token)
    else:
        st.error("⚠️ Missing Service Account secrets! Cannot load or save slug mappings.")

    # --- Sidebar ---
    with st.sidebar:
        st.header("⚙️ Task Configuration")
        eng_branch     = st.text_input("Engineering Branch", value="master")
        target_version = st.text_input("ReadMe Version", value="v2026.5.0-0")
        st.divider()
        st.caption(f"🔒 Eng Repo: `{eng_repo_url}`")
        st.caption(f"📂 App Repo: `{app_repo_name}`")

    # --- Pull specs button ---
    if st.button(f"📥 1. Pull Specs from `{eng_branch}`"):
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)
        workspace_dir.mkdir()
        parsed   = urllib.parse.urlparse(eng_repo_url)
        auth_url = urllib.parse.urlunparse((
            parsed.scheme,
            f"{git_user}:{git_token}@{parsed.netloc}",
            parsed.path, "", "", ""
        ))
        with st.spinner("Cloning engineering repo..."):
            p = subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", eng_branch, auth_url, str(workspace_dir)],
                capture_output=True
            )
            if p.returncode == 0:
                st.success("✅ Specs pulled.")
            else:
                st.error(f"❌ Error: {p.stderr.decode()}")

    st.divider()
    npx = shutil.which("npx")
    tab_git, tab_manual, tab_mintlify = st.tabs([
        "🐙 Git Repo Pipeline",
        "📂 Manual File Upload",
        "🌿 Pull to Mintlify"
    ])

    # =========================================================================
    # TAB 1 — GIT REPO PIPELINE
    # =========================================================================
    with tab_git:
        st.subheader("🛠️ 2. Select API Spec")
        yaml_files = []
        for p in [path_main, path_logical]:
            tp = workspace_dir / p
            if tp.exists():
                yaml_files.extend(
                    f for f in tp.glob("*.yaml") if not f.name.endswith("_prepped.yaml")
                )

        file_options = sorted(f.name for f in yaml_files)

        if not file_options:
            st.info("👈 Please click '1. Pull Specs' above to load files from the repository.")
        else:
            selected_file_name = st.selectbox("Select Spec", file_options)
            selected_file_path = next(f for f in yaml_files if f.name == selected_file_name)

            mapped_id  = current_mapping.get(selected_file_path.stem, "")
            is_new_file = False

            if not mapped_id:
                is_new_file = True
                try:
                    with open(selected_file_path, "r") as f:
                        temp_data = yaml.safe_load(f)
                    raw_title = temp_data.get("info", {}).get("title", selected_file_path.stem)
                    mapped_id = re.sub(r"[^a-z0-9]+", "-", raw_title.lower()).strip("-")
                except Exception:
                    mapped_id = selected_file_path.stem

            col1, col2 = st.columns(2)
            col1.info(f"**Original File:** `{selected_file_name}`")
            if is_new_file:
                col2.warning(f"**Auto-Generated Slug:** `{mapped_id}`")
            elif mapped_id:
                col2.success(f"**Mapped Slug:** `{mapped_id}`")

            final_id = st.text_input("Target ReadMe Slug (Filename):", value=mapped_id)

            st.divider()
            st.subheader("🚀 3. Choose Action")
            col_v, col_u = st.columns(2)

            with col_v:
                if st.button("🔍 Run Validations Only"):
                    prepped  = prep_openapi_file(selected_file_path, target_version, final_id)
                    abs_cwd  = str(prepped.parent.resolve())
                    st.write("### 🔍 Logs")
                    run_command_ui(f"{npx} --yes swagger-cli validate {prepped.name}", cwd=abs_cwd)
                    run_command_ui(f"{npx} --yes rdme openapi validate {prepped.name}", cwd=abs_cwd)

            with col_u:
                if st.button("☁️ Validate & Upload", type="primary"):
                    if not final_id.strip():
                        st.error("❌ Target ReadMe Slug cannot be empty.")
                    else:
                        prepped = prep_openapi_file(selected_file_path, target_version, final_id)
                        abs_cwd = str(prepped.parent.resolve())
                        st.write("### 🔍 Logs")
                        v1 = run_command_ui(f"{npx} --yes swagger-cli validate {prepped.name}", cwd=abs_cwd)
                        v2 = run_command_ui(f"{npx} --yes rdme openapi validate {prepped.name}", cwd=abs_cwd)

                        if v2 == 0:
                            if v1 != 0:
                                st.warning("⚠️ Swagger-CLI flagged issues, but ReadMe validation passed. Proceeding...")
                            else:
                                st.success(f"✅ Validations passed. Uploading as `{prepped.name}`...")

                            upload_cmd = (
                                f"{npx} --yes rdme openapi upload {prepped.name} "
                                f"--key {readme_key} --slug {final_id}.json --branch {target_version}"
                            )
                            if run_command_ui(upload_cmd, cwd=abs_cwd, mask_secrets=[readme_key]) == 0:
                                st.success("🎉 Successfully uploaded to ReadMe!")
                                if is_new_file:
                                    with st.spinner("Pushing new slug to App repo..."):
                                        current_mapping[selected_file_path.stem] = final_id
                                        if save_slug_mapping(app_repo_name, svc_git_token, current_mapping, current_sha):
                                            st.success(f"📝 Added `'{selected_file_path.stem}': '{final_id}'` to `slug_mapping.json`.")
                                        else:
                                            st.warning("⚠️ Upload succeeded, but failed to save the mapping to GitHub.")
                            else:
                                st.error("❌ Upload failed. See logs above.")

    # =========================================================================
    # TAB 2 — MANUAL FILE UPLOAD
    # =========================================================================
    with tab_manual:
        st.subheader("📂 Manual File Override")
        st.info("Upload your modified YAML or JSON spec. **Note:** You must 'Pull Specs' first so the app has the external `$ref` dependency files to validate against!")

        if not list(workspace_dir.glob("**/*.yaml")):
            st.warning("⚠️ Please click '1. Pull Specs' in the sidebar first to load the dependency schemas.")
        else:
            manual_file = st.file_uploader(
                "Upload your modified YAML or JSON spec", type=["yaml", "yml", "json"]
            )

            if manual_file is not None:
                target_paths = list(workspace_dir.rglob(manual_file.name))

                if not target_paths:
                    st.info(f"ℹ️ `{manual_file.name}` not found in the repository. Treating as a standalone file.")
                    manual_path = workspace_dir / manual_file.name
                    with open(manual_path, "wb") as f:
                        f.write(manual_file.getbuffer())
                else:
                    manual_path = target_paths[0]
                    with open(manual_path, "wb") as f:
                        f.write(manual_file.getbuffer())
                    st.success(f"✅ Injected into `{manual_path.relative_to(workspace_dir)}`")

                manual_mapped_id = current_mapping.get(manual_path.stem, "")
                is_manual_new    = False

                if not manual_mapped_id:
                    is_manual_new = True
                    try:
                        with open(manual_path, "r") as f:
                            temp_data = yaml.safe_load(f)
                        raw_title        = temp_data.get("info", {}).get("title", manual_path.stem)
                        manual_mapped_id = re.sub(r"[^a-z0-9]+", "-", raw_title.lower()).strip("-")
                    except Exception:
                        manual_mapped_id = manual_path.stem

                manual_final_id = st.text_input(
                    "Target ReadMe Slug (Manual):", value=manual_mapped_id, key="manual_slug_input"
                )

                col_mv, col_mu = st.columns(2)

                with col_mv:
                    if st.button("🔍 Validate Custom Spec"):
                        manual_prepped = prep_openapi_file(manual_path, target_version, manual_final_id)
                        abs_cwd        = str(manual_prepped.parent.resolve())
                        st.write("### 🔍 Logs")
                        run_command_ui(f"{npx} --yes swagger-cli validate {manual_prepped.name}", cwd=abs_cwd)
                        run_command_ui(f"{npx} --yes rdme openapi validate {manual_prepped.name}", cwd=abs_cwd)

                with col_mu:
                    if st.button("☁️ Validate & Upload Custom Spec", type="primary"):
                        if not manual_final_id.strip():
                            st.error("❌ Target ReadMe Slug cannot be empty.")
                        else:
                            manual_prepped = prep_openapi_file(manual_path, target_version, manual_final_id)
                            abs_cwd        = str(manual_prepped.parent.resolve())
                            st.write("### 🔍 Logs")
                            v1 = run_command_ui(f"{npx} --yes swagger-cli validate {manual_prepped.name}", cwd=abs_cwd)
                            v2 = run_command_ui(f"{npx} --yes rdme openapi validate {manual_prepped.name}", cwd=abs_cwd)

                            if v2 == 0:
                                if v1 != 0:
                                    st.warning("⚠️ Swagger-CLI flagged issues, but ReadMe validation passed. Proceeding...")
                                else:
                                    st.success(f"✅ Validations passed. Uploading `{manual_prepped.name}`...")

                                upload_cmd = (
                                    f"{npx} --yes rdme openapi upload {manual_prepped.name} "
                                    f"--key {readme_key} --slug {manual_final_id}.json --branch {target_version}"
                                )
                                if run_command_ui(upload_cmd, cwd=abs_cwd, mask_secrets=[readme_key]) == 0:
                                    st.success("🎉 Successfully uploaded Custom File to ReadMe!")
                                    if is_manual_new:
                                        with st.spinner("Pushing new slug to App repo..."):
                                            current_mapping[manual_path.stem] = manual_final_id
                                            if save_slug_mapping(app_repo_name, svc_git_token, current_mapping, current_sha):
                                                st.success(f"📝 Added `'{manual_path.stem}': '{manual_final_id}'` to `slug_mapping.json`.")
                                else:
                                    st.error("❌ Upload failed. See logs above.")

    # =========================================================================
    # TAB 3 — PULL TO MINTLIFY
    # =========================================================================
    with tab_mintlify:
        st.subheader("🌿 Pull Specs from ReadMe → Mintlify Branch")
        st.info(
            "Fetches all specs for the selected ReadMe versions, commits them to "
            f"`{MINTLIFY_BRANCH}` in `{mintlify_repo}`, generates MDX pages via the "
            "Mintlify scraper, and patches `docs.json` with the correct navigation structure."
        )
        st.caption(f"🎯 Target repo: `{mintlify_repo}` → branch: `{MINTLIFY_BRANCH}`")

        selected_versions = st.multiselect(
            "Select ReadMe versions to pull",
            options=list(VERSION_MAP.keys()),
            format_func=lambda v: f"{VERSION_MAP[v]}  ({v})",
            default=["v2026.5.0-0"],
            help="ReadMe branch slug → canonical display version (e.g. 2026.5.0.0)"
        )

        if st.button("⬇️ Pull Specs & Update docs.json", type="primary"):
            # --- Pre-flight checks ---
            if not selected_versions:
                st.error("❌ Please select at least one version.")
                st.stop()
            if not mintlify_repo:
                st.error("❌ MINTLIFY_REPO_NAME secret is missing.")
                st.stop()
            if not git_token:
                st.error("❌ GIT_TOKEN secret is missing.")
                st.stop()
            if not readme_key:
                st.error("❌ README_API_KEY secret is missing.")
                st.stop()

            gh_headers = {
                "Authorization": f"token {git_token}",
                "Accept": "application/vnd.github.v3+json"
            }

            # Deduplicate slugs (cde appears twice in slug_mapping)
            seen_slugs     = set()
            unique_mapping = {}
            for k, v in current_mapping.items():
                if v not in seen_slugs:
                    seen_slugs.add(v)
                    unique_mapping[k] = v

            all_tab_groups = []  # one entry per version dropdown
            any_failures   = False

            for readme_version in selected_versions:
                display_version = VERSION_MAP[readme_version]
                st.markdown(f"---\n#### 📦 `{display_version}` (ReadMe: `{readme_version}`)")

                version_groups = []
                skipped        = []
                failed         = []

                for eng_key, readme_slug in unique_mapping.items():

                    # ----------------------------------------------------------
                    # STEP 1: Fetch spec metadata from ReadMe v2 API
                    # GET /branches/{branch}/apis/{filename}.json
                    # ----------------------------------------------------------
                    fetch_url = (
                        f"https://api.readme.com/v2/branches/{readme_version}"
                        f"/apis/{readme_slug}.json"
                    )
                    resp = requests.get(
                        fetch_url,
                        headers={"Authorization": f"Bearer {readme_key}"}
                    )

                    if resp.status_code == 404:
                        skipped.append(readme_slug)
                        continue

                    if resp.status_code != 200:
                        failed.append(readme_slug)
                        any_failures = True
                        st.error(f"❌ Error fetching `{readme_slug}` ({resp.status_code}): {resp.text[:200]}")
                        continue

                    # ----------------------------------------------------------
                    # STEP 2: Get raw spec content.
                    # The v2 GET /branches/{branch}/apis/{slug}.json returns a
                    # metadata wrapper {"data": {...}}, NOT the raw spec.
                    # Use rdme CLI to download the actual raw spec file.
                    # ----------------------------------------------------------
                    resp_data  = resp.json().get("data", {})
                    source_url = (resp_data.get("source") or {}).get("url")

                    if source_url:
                        # Uploaded via URL — fetch raw spec directly
                        spec_content = requests.get(source_url).content
                    else:
                        # CLI-uploaded — use rdme to download the raw spec
                        local_dl_dir  = Path(f"./mintlify_scratch/{display_version}")
                        local_dl_dir.mkdir(parents=True, exist_ok=True)
                        local_dl_path = local_dl_dir / f"{readme_slug}.json"

                        dl_cmd = (
                            f"npx --yes rdme openapi download "
                            f"--key {readme_key} "
                            f"--slug {readme_slug}.json "
                            f"--branch {readme_version} "
                            f"--out {local_dl_path}"
                        )
                        dl_result = subprocess.run(
                            dl_cmd, shell=True,
                            capture_output=True, text=True,
                            env={**os.environ, "CI": "true"}
                        )

                        if dl_result.returncode == 0 and local_dl_path.exists():
                            spec_content = local_dl_path.read_bytes()
                        else:
                            failed.append(readme_slug)
                            any_failures = True
                            st.error(
                                f"❌ `{readme_slug}`: rdme download failed. "
                                f"stderr: {dl_result.stderr[:300]}"
                            )
                            continue

                    # Sanity check: confirm we have a real OpenAPI spec
                    try:
                        spec_json = json.loads(spec_content)
                        if "openapi" not in spec_json and "swagger" not in spec_json:
                            failed.append(readme_slug)
                            any_failures = True
                            st.error(
                                f"❌ `{readme_slug}`: fetched content is not a valid OpenAPI spec "
                                f"(missing `openapi`/`swagger` field)."
                            )
                            continue
                    except json.JSONDecodeError:
                        pass  # YAML spec — fine, scraper handles it

                    # ----------------------------------------------------------
                    # STEP 3: Commit spec JSON to Mintlify branch
                    # ----------------------------------------------------------
                    spec_repo_path = f"{API_REF_BASE}/{display_version}/{readme_slug}.json"
                    ok, put_resp   = commit_file_to_branch(
                        repo          = mintlify_repo,
                        token         = git_token,
                        branch        = MINTLIFY_BRANCH,
                        file_path     = spec_repo_path,
                        content_bytes = spec_content,
                        message       = f"🤖 Pull {readme_slug} for {display_version} from ReadMe"
                    )

                    if not ok:
                        failed.append(readme_slug)
                        any_failures = True
                        st.error(
                            f"❌ Failed to commit `{readme_slug}`: "
                            f"{put_resp.json().get('message', put_resp.text)}"
                        )
                        continue

                    # ----------------------------------------------------------
                    # STEP 4: Run Mintlify scraper to generate MDX files,
                    # then commit each generated MDX to the branch.
                    # ----------------------------------------------------------
                    local_spec_dir  = Path(f"./mintlify_scratch/{display_version}")
                    local_spec_dir.mkdir(parents=True, exist_ok=True)
                    local_spec_path = local_spec_dir / f"{readme_slug}.json"
                    local_spec_path.write_bytes(spec_content)

                    mdx_out_dir = local_spec_dir / readme_slug
                    mdx_out_dir.mkdir(exist_ok=True)

                    scrape_cmd = (
                        f"npx --yes @mintlify/scraping@latest openapi-file "
                        f"{local_spec_path} -o {mdx_out_dir}"
                    )
                    result = subprocess.run(
                        scrape_cmd, shell=True,
                        capture_output=True, text=True,
                        env={**os.environ, "CI": "true"}
                    )

                    mdx_files    = list(mdx_out_dir.glob("*.mdx"))
                    mdx_committed = 0

                    for mdx_file in mdx_files:
                        mdx_repo_path = (
                            f"{API_REF_BASE}/{display_version}"
                            f"/{readme_slug}/{mdx_file.name}"
                        )
                        mdx_ok, _ = commit_file_to_branch(
                            repo          = mintlify_repo,
                            token         = git_token,
                            branch        = MINTLIFY_BRANCH,
                            file_path     = mdx_repo_path,
                            content_bytes = mdx_file.read_bytes(),
                            message       = (
                                f"🤖 Generate MDX for {readme_slug}/{mdx_file.name} "
                                f"({display_version})"
                            )
                        )
                        if mdx_ok:
                            mdx_committed += 1

                    if mdx_files:
                        st.success(
                            f"✅ `{readme_slug}`: spec committed, "
                            f"{mdx_committed}/{len(mdx_files)} MDX pages generated"
                        )
                    else:
                        # Scraper produced nothing — log warning but still add the group
                        st.warning(
                            f"⚠️ `{readme_slug}`: spec committed but scraper produced no MDX. "
                            f"Scraper stderr: {result.stderr[:300] or '(none)'}"
                        )

                    # Build the navigation group entry.
                    # Use source+directory so Mintlify auto-generates pages from the spec.
                    version_groups.append({
                        "group": readme_slug.replace("-", " ").title(),
                        "openapi": {
                            "source": f"api-reference/{display_version}/{readme_slug}.json",
                            "directory": f"api-reference/{display_version}/{readme_slug}"
                        }
                    })

                # Per-version summary
                st.success(f"✅ {len(version_groups)} spec(s) processed for `{display_version}`")
                if skipped:
                    st.info(
                        f"ℹ️ Skipped {len(skipped)} spec(s) not in `{readme_version}`: "
                        + ", ".join(f"`{s}`" for s in skipped)
                    )
                if failed:
                    st.warning(
                        f"⚠️ {len(failed)} spec(s) failed for `{readme_version}`: "
                        + ", ".join(f"`{s}`" for s in failed)
                    )

                # Wrap this version's groups in a dropdown entry
                all_tab_groups.append({
                    "dropdown": display_version,
                    "groups":   version_groups
                })

            # ------------------------------------------------------------------
            # STEP 5: Patch docs.json
            # Use tab → dropdowns → [{ dropdown, groups }] structure as
            # recommended by Mintlify, with source+directory on each group
            # so MDX pages are auto-generated at build time as a fallback.
            # ------------------------------------------------------------------
            st.markdown("---\n#### 📝 Patching `docs.json`")

            docs_url      = f"https://api.github.com/repos/{mintlify_repo}/contents/{DOCS_JSON_PATH}"
            docs_resp     = gh_get(docs_url, git_token, params={"ref": MINTLIFY_BRANCH})

            if docs_resp.status_code != 200:
                st.error(
                    f"❌ Could not fetch `docs.json` from `{MINTLIFY_BRANCH}`: "
                    f"{docs_resp.json().get('message', '')}"
                )
            else:
                docs_data = json.loads(base64.b64decode(docs_resp.json()["content"]))
                docs_sha  = docs_resp.json()["sha"]

                # Find API Reference tab and replace its content cleanly
                patched = False
                for tab in docs_data.get("navigation", {}).get("tabs", []):
                    if tab.get("tab") == "API Reference":
                        # Remove any previously written keys
                        for key in ["groups", "pages", "versions", "dropdowns"]:
                            tab.pop(key, None)
                        tab["dropdowns"] = all_tab_groups
                        patched = True
                        break

                if not patched:
                    st.error(
                        "❌ Could not find an `API Reference` tab in `docs.json`. "
                        "Verify the tab name matches exactly."
                    )
                else:
                    ok, put_resp = commit_file_to_branch(
                        repo          = mintlify_repo,
                        token         = git_token,
                        branch        = MINTLIFY_BRANCH,
                        file_path     = DOCS_JSON_PATH,
                        content_bytes = json.dumps(docs_data, indent=2).encode("utf-8"),
                        message       = (
                            "🤖 Update docs.json API Reference for: "
                            + ", ".join(VERSION_MAP[v] for v in selected_versions)
                        )
                    )

                    if ok:
                        if any_failures:
                            st.warning(
                                "⚠️ `docs.json` updated, but some specs had errors. "
                                "Review the log above before merging."
                            )
                        else:
                            st.success(
                                f"🎉 All done! `docs.json` committed to `{MINTLIFY_BRANCH}`. "
                                "Mintlify will auto-deploy the branch preview shortly."
                            )
                    else:
                        st.error(
                            f"❌ Failed to update `docs.json`: "
                            f"{put_resp.json().get('message', put_resp.text)}"
                        )

            # Cleanup local scratch directory
            shutil.rmtree("./mintlify_scratch", ignore_errors=True)


if __name__ == "__main__":
    main()
