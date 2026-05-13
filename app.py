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

def gh_delete(url, token, payload):
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    return requests.delete(url, headers=headers, json=payload)

def load_slug_mapping(repo_name, token):
    url  = f"https://api.github.com/repos/{repo_name}/contents/slug_mapping.json"
    resp = gh_get(url, token)
    if resp.status_code == 200:
        data    = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return json.loads(content), data["sha"]
    elif resp.status_code == 404:
        return {}, None
    st.error(f"⚠️ Failed to load slug mapping: {resp.text}")
    return {}, None

def save_slug_mapping(repo_name, token, updated_mapping, sha):
    url     = f"https://api.github.com/repos/{repo_name}/contents/slug_mapping.json"
    encoded = base64.b64encode(json.dumps(updated_mapping, indent=4).encode("utf-8")).decode("utf-8")
    payload = {"message": "🤖 Auto-update: Added new API slug mapping", "content": encoded, "branch": "main"}
    if sha:
        payload["sha"] = sha
    resp = gh_put(url, token, payload)
    return resp.status_code in [200, 201]

def commit_file_to_branch(repo, token, branch, file_path, content_bytes, message, retries=3):
    """Creates or updates a file on a GitHub branch. Retries on SHA conflict (409 or 422)."""
    url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
    for attempt in range(retries + 1):
        existing = gh_get(url, token, params={"ref": branch})
        sha      = existing.json().get("sha") if existing.status_code == 200 else None
        payload  = {
            "message": message,
            "content": base64.b64encode(content_bytes).decode("utf-8"),
            "branch":  branch,
        }
        if sha:
            payload["sha"] = sha
        resp = gh_put(url, token, payload)
        if resp.status_code in [200, 201]:
            return True, resp
        if resp.status_code in [409, 422] and attempt < retries:
            continue
        return False, resp
    return False, resp

def batch_commit_files(repo, token, branch, files, message):
    """
    Commits multiple files in a single Git commit using the Trees API.
    One commit = one Mintlify build trigger instead of one per file.

    files: list of {"path": "repo/relative/path", "content": bytes}
    Returns (success: bool, error_message: str|None)
    """
    import urllib.parse
    base_url      = f"https://api.github.com/repos/{repo}"
    headers       = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    encoded_branch = urllib.parse.quote(branch, safe="")

    # Get current branch HEAD SHA
    ref_resp = requests.get(f"{base_url}/git/ref/heads/{encoded_branch}", headers=headers)
    if ref_resp.status_code != 200:
        return False, f"Could not get branch ref: {ref_resp.text}"
    base_commit_sha = ref_resp.json()["object"]["sha"]

    # Get base tree SHA from HEAD commit
    commit_resp = requests.get(f"{base_url}/git/commits/{base_commit_sha}", headers=headers)
    if commit_resp.status_code != 200:
        return False, f"Could not get base commit: {commit_resp.text}"
    base_tree_sha = commit_resp.json()["tree"]["sha"]

    # Create a blob for each file
    tree_items = []
    for file in files:
        blob_resp = requests.post(
            f"{base_url}/git/blobs",
            headers=headers,
            json={"content": base64.b64encode(file["content"]).decode("utf-8"), "encoding": "base64"},
        )
        if blob_resp.status_code not in [200, 201]:
            return False, f"Could not create blob for {file['path']}: {blob_resp.text}"
        tree_items.append({"path": file["path"], "mode": "100644", "type": "blob", "sha": blob_resp.json()["sha"]})

    # Create new tree
    tree_resp = requests.post(
        f"{base_url}/git/trees",
        headers=headers,
        json={"base_tree": base_tree_sha, "tree": tree_items},
    )
    if tree_resp.status_code not in [200, 201]:
        return False, f"Could not create tree: {tree_resp.text}"
    new_tree_sha = tree_resp.json()["sha"]

    # Create new commit
    new_commit_resp = requests.post(
        f"{base_url}/git/commits",
        headers=headers,
        json={"message": message, "tree": new_tree_sha, "parents": [base_commit_sha]},
    )
    if new_commit_resp.status_code not in [200, 201]:
        return False, f"Could not create commit: {new_commit_resp.text}"
    new_commit_sha = new_commit_resp.json()["sha"]

    # Update branch HEAD — use refs endpoint with encoded branch
    update_resp = requests.patch(
        f"{base_url}/git/refs/heads/{encoded_branch}",
        headers=headers,
        json={"sha": new_commit_sha, "force": False},
    )
    if update_resp.status_code not in [200, 201]:
        return False, f"Could not update branch ref: {update_resp.text}"

    return True, None

# ---------------------------------------------------------------------------
# README API v2 HELPERS
# ---------------------------------------------------------------------------

def readme_branch(readme_version):
    return readme_version.lstrip("v")

def readme_get(path, readme_key, params=None):
    return requests.get(
        f"https://api.readme.com/v2{path}",
        headers={"Authorization": f"Bearer {readme_key}"},
        params=params,
    )

def get_branch_api_slugs(readme_version, readme_key):
    resp = readme_get(f"/branches/{readme_branch(readme_version)}/apis", readme_key)
    if resp.status_code != 200:
        return set(), resp.text
    return {item["filename"] for item in resp.json().get("data", [])}, None

def get_branch_reference_categories(readme_version, readme_key):
    resp = readme_get(f"/branches/{readme_branch(readme_version)}/categories/reference", readme_key)
    if resp.status_code != 200:
        return [], resp.text
    cats = resp.json().get("data", [])
    return sorted(cats, key=lambda c: c.get("position", 0)), None

def get_category_pages(readme_version, category_title, readme_key):
    resp = readme_get(
        f"/branches/{readme_branch(readme_version)}/categories/reference/{category_title}/pages",
        readme_key,
    )
    if resp.status_code != 200:
        return [], resp.text
    pages = resp.json().get("data", [])
    return sorted(pages, key=lambda p: p.get("position", 0)), None

def get_reference_page(readme_version, page_slug, readme_key):
    resp = readme_get(
        f"/branches/{readme_branch(readme_version)}/reference/{page_slug}",
        readme_key,
    )
    if resp.status_code != 200:
        return None
    return resp.json().get("data", {})

# ---------------------------------------------------------------------------
# NODE.JS SETUP
# ---------------------------------------------------------------------------

def ensure_node_installed():
    node_version  = "v20.17.0"
    install_dir   = Path("./node_runtime")
    node_dirname  = f"node-{node_version}-linux-x64"
    node_bin_path = install_dir / node_dirname / "bin"
    try:
        if subprocess.run(["node", "-v"], capture_output=True).returncode == 0:
            return
    except FileNotFoundError:
        pass
    if not node_bin_path.exists():
        with st.spinner("🔧 Initializing environment (Node.js)..."):
            url      = f"https://nodejs.org/dist/{node_version}/{node_dirname}.tar.xz"
            resp     = requests.get(url, stream=True)
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
    run_env       = os.environ.copy()
    run_env["CI"] = "true"
    process = subprocess.Popen(
        cmd_string, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=cwd, env=run_env,
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
# OPENAPI FILE PREP
# ---------------------------------------------------------------------------

def prep_openapi_file(filepath, version, target_slug):
    """For Tabs 1 & 2: writes a prepped YAML file for CLI validation/upload to ReadMe."""
    with open(filepath, "r") as f:
        data = yaml.safe_load(f)
    data.setdefault("info", {})["version"] = version
    data.setdefault("x-readme", {}).update({"explorer-enabled": False, "proxy-enabled": True})
    for server in data.get("servers", []):
        variables = server.get("variables", {})
        if "protocol" in variables:
            variables["protocol"]["default"] = "https"
        if "base-url" in variables:
            variables["base-url"]["default"] = "alation_domain"
    yaml_filepath = filepath.parent / f"{target_slug}_prepped.yaml"
    with open(yaml_filepath, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    return yaml_filepath

def prep_spec_content(filepath, version, readme_slug):
    """For Tab 3: loads YAML, applies prep transformations, returns YAML bytes."""
    with open(filepath, "r") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"YAML did not parse to a dict — got {type(data)}")
    data.setdefault("info", {})["version"] = version
    data.setdefault("x-readme", {}).update({"explorer-enabled": False, "proxy-enabled": True})
    for server in data.get("servers", []):
        variables = server.get("variables", {})
        if "protocol" in variables:
            variables["protocol"]["default"] = "https"
        if "base-url" in variables:
            variables["base-url"]["default"] = "alation_domain"
    return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True).encode("utf-8")

def prep_spec_from_dict(data, version):
    """For ReadMe fallback: applies prep transformations to an already-loaded dict."""
    if not isinstance(data, dict):
        return None
    data.setdefault("info", {})["version"] = version
    data.setdefault("x-readme", {}).update({"explorer-enabled": False, "proxy-enabled": True})
    for server in data.get("servers", []):
        variables = server.get("variables", {})
        if "protocol" in variables:
            variables["protocol"]["default"] = "https"
        if "base-url" in variables:
            variables["base-url"]["default"] = "alation_domain"
    return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True).encode("utf-8")

# ---------------------------------------------------------------------------
# MDX BUILDER — content/overview pages only
# ---------------------------------------------------------------------------

def slug_to_mdx_filename(slug):
    return re.sub(r"[^a-z0-9-]", "-", slug.lower()).strip("-") + ".mdx"

def build_content_mdx(page_title, body=""):
    """MDX for non-endpoint pages (overview, authentication, custom content)."""
    safe_title = page_title.replace('"', '\\"')
    content    = body.strip() if body else ""
    return (
        f'---\n'
        f'title: "{safe_title}"\n'
        f'---\n\n'
        f'{content}\n'
    ).encode("utf-8")

# ---------------------------------------------------------------------------
# MINTLIFY CONSTANTS
# ---------------------------------------------------------------------------

MINTLIFY_BRANCH = "elena/testNavigationChanges"
DOCS_JSON_PATH  = "mintlify-poc-docs/docs.json"
API_REF_BASE    = "mintlify-poc-docs/api-reference"

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

    reverse_mapping = {}
    for eng_key, readme_slug in current_mapping.items():
        reverse_mapping.setdefault(readme_slug, []).append(eng_key)

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
            parsed.scheme, f"{git_user}:{git_token}@{parsed.netloc}",
            parsed.path, "", "", ""
        ))
        with st.spinner("Cloning engineering repo..."):
            p = subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", eng_branch, auth_url, str(workspace_dir)],
                capture_output=True,
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
        "🌿 Pull to Mintlify",
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
                yaml_files.extend(f for f in tp.glob("*.yaml") if not f.name.endswith("_prepped.yaml"))

        file_options = sorted(f.name for f in yaml_files)

        if not file_options:
            st.info("👈 Please click '1. Pull Specs' above to load files from the repository.")
        else:
            selected_file_name = st.selectbox("Select Spec", file_options)
            selected_file_path = next(f for f in yaml_files if f.name == selected_file_name)
            mapped_id   = current_mapping.get(selected_file_path.stem, "")
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
                    prepped = prep_openapi_file(selected_file_path, target_version, final_id)
                    abs_cwd = str(prepped.parent.resolve())
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
                                            st.warning("⚠️ Upload succeeded, but failed to save the mapping.")
                            else:
                                st.error("❌ Upload failed. See logs above.")

    # =========================================================================
    # TAB 2 — MANUAL FILE UPLOAD
    # =========================================================================
    with tab_manual:
        st.subheader("📂 Manual File Override")
        st.info("Upload your modified YAML or JSON spec. **Note:** You must 'Pull Specs' first so the app has the external `$ref` dependency files to validate against!")

        if not list(workspace_dir.glob("**/*.yaml")):
            st.warning("⚠️ Please click '1. Pull Specs' first to load dependency schemas.")
        else:
            manual_file = st.file_uploader("Upload your modified YAML or JSON spec", type=["yaml", "yml", "json"])
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

                manual_final_id = st.text_input("Target ReadMe Slug (Manual):", value=manual_mapped_id, key="manual_slug_input")

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
        st.subheader("🌿 Migrate ReadMe → Mintlify Branch")
        st.info(
            "For each selected version this tab will:\n"
            "1. Pull spec list and category structure from ReadMe v2 API\n"
            "2. Source spec content from the engineering repo YAML (with ReadMe fallback)\n"
            "3. Commit spec YAML files to the Mintlify branch\n"
            "4. Fetch overview/content pages from ReadMe and commit as MDX\n"
            "5. Patch `docs.json` — Mintlify auto-generates endpoint pages from each spec"
        )
        st.caption(f"🎯 Target: `{mintlify_repo}` → `{MINTLIFY_BRANCH}`")

        selected_versions = st.multiselect(
            "Select ReadMe versions to migrate",
            options=list(VERSION_MAP.keys()),
            format_func=lambda v: f"{VERSION_MAP[v]}  ({v})",
            default=["v2026.5.0-0"],
            help="ReadMe branch slug → canonical display version",
        )

        # --- Debug expander ---
        with st.expander("🔬 Debug: Inspect ReadMe API responses"):
            st.caption("Inspect raw ReadMe API responses to verify field availability.")
            debug_version = st.selectbox(
                "Version to inspect",
                options=list(VERSION_MAP.keys()),
                format_func=lambda v: f"{VERSION_MAP[v]}  ({v})",
                key="debug_version",
            )
            col_d1, col_d2, col_d3 = st.columns(3)
            with col_d1:
                if st.button("📋 Inspect Spec List"):
                    resp = readme_get(f"/branches/{readme_branch(debug_version)}/apis", readme_key)
                    st.write(f"**Status:** {resp.status_code}")
                    st.json(resp.json())
            with col_d2:
                if st.button("📂 Inspect Categories"):
                    resp = readme_get(f"/branches/{readme_branch(debug_version)}/categories/reference", readme_key)
                    st.write(f"**Status:** {resp.status_code}")
                    st.json(resp.json())
            with col_d3:
                debug_cat = st.text_input("Category title to inspect pages", key="debug_cat")
                if st.button("📄 Inspect Category Pages") and debug_cat:
                    resp = readme_get(
                        f"/branches/{readme_branch(debug_version)}/categories/reference/{debug_cat}/pages",
                        readme_key,
                    )
                    st.write(f"**Status:** {resp.status_code}")
                    st.json(resp.json())
            debug_slug = st.text_input("Reference page slug to inspect", key="debug_slug")
            if st.button("🔍 Inspect Single Reference Page") and debug_slug:
                resp = readme_get(
                    f"/branches/{readme_branch(debug_version)}/reference/{debug_slug}",
                    readme_key,
                )
                st.write(f"**Status:** {resp.status_code}")
                st.json(resp.json())

        if st.button("⬇️ Migrate to Mintlify", type="primary"):

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
            if not list(workspace_dir.glob("**/*.yaml")):
                st.error("❌ Engineering repo not pulled. Click '1. Pull Specs' first.")
                st.stop()

            all_version_dropdowns = []
            any_failures          = False
            all_files             = []  # all files staged for batch commit

            for readme_version in selected_versions:
                display_version = VERSION_MAP[readme_version]
                st.markdown(f"---\n#### 📦 `{display_version}` (ReadMe: `{readme_version}`)")

                # ==============================================================
                # STEP 1: Get spec list from ReadMe
                # ==============================================================
                with st.spinner("Fetching spec list from ReadMe..."):
                    branch_slugs, err = get_branch_api_slugs(readme_version, readme_key)
                if err:
                    st.error(f"❌ Failed to fetch spec list: {err}")
                    any_failures = True
                    continue
                st.write(f"📋 Found **{len(branch_slugs)}** spec(s) in ReadMe for this version")

                # ==============================================================
                # STEP 2: Get reference categories from ReadMe
                # ==============================================================
                with st.spinner("Fetching reference categories from ReadMe..."):
                    categories, err = get_branch_reference_categories(readme_version, readme_key)
                if err:
                    st.error(f"❌ Failed to fetch categories: {err}")
                    any_failures = True
                    continue
                st.write(f"📂 Found **{len(categories)}** reference categories")

                # ==============================================================
                # STEP 3: Commit spec YAML files to Mintlify branch.
                # Priority:
                #   1. Engineering repo YAML (via slug_mapping)
                #   2. ReadMe API direct fetch (fallback for manually uploaded specs)
                # Build spec_path_index in memory for endpoint detection in Step 4.
                # ==============================================================
                committed_specs = {}  # readme_slug → "api-reference/{ver}/{slug}.yaml"
                spec_path_index = {}  # readme_slug → {"/path/": {"method": op, ...}}
                skipped_specs   = []

                for filename in sorted(branch_slugs):
                    readme_slug  = re.sub(r"\.(json|yaml|yml)$", "", filename)
                    eng_keys     = reverse_mapping.get(readme_slug)
                    spec_content = None
                    used_source  = None

                    # Source 1: Engineering repo YAML
                    if eng_keys:
                        for eng_key in eng_keys:
                            for spec_dir in [path_main, path_logical]:
                                candidate = workspace_dir / spec_dir / f"{eng_key}.yaml"
                                if candidate.exists():
                                    try:
                                        spec_content = prep_spec_content(candidate, display_version, readme_slug)
                                        used_source  = f"{eng_key}.yaml"
                                    except Exception as e:
                                        st.error(f"❌ `{readme_slug}`: prep failed — {e}")
                                    break
                            if spec_content is not None:
                                break
                        if spec_content is None:
                            tried = ", ".join(f"`{k}.yaml`" for k in eng_keys)
                            st.warning(f"⚠️ `{readme_slug}`: not in eng repo ({tried}) — trying ReadMe...")

                    # Source 2: ReadMe API fallback
                    if spec_content is None:
                        try:
                            r = requests.get(
                                f"https://api.readme.com/v2/branches/{readme_branch(readme_version)}/apis/{readme_slug}.json",
                                headers={"Authorization": f"Bearer {readme_key}"},
                            )
                            if r.status_code == 200:
                                spec_content = prep_spec_from_dict(r.json(), display_version)
                                if spec_content:
                                    used_source = "ReadMe (manually uploaded)"
                        except Exception as e:
                            st.warning(f"⚠️ `{readme_slug}`: ReadMe fallback failed — {e}")

                    if spec_content is None:
                        skipped_specs.append(readme_slug)
                        st.warning(f"⚠️ `{readme_slug}`: not found in eng repo or ReadMe — skipping.")
                        continue

                    # Stage spec for batch commit
                    spec_repo_path = f"{API_REF_BASE}/{display_version}/{readme_slug}.yaml"
                    all_files.append({"path": spec_repo_path, "content": spec_content})
                    committed_specs[readme_slug] = f"/api-reference/{display_version}/{readme_slug}.yaml"
                    # Build path index in memory
                    try:
                        spec_path_index[readme_slug] = yaml.safe_load(spec_content).get("paths", {})
                    except Exception:
                        spec_path_index[readme_slug] = {}

                st.write(f"📦 Prepared **{len(committed_specs)}** spec(s) for commit")
                if skipped_specs:
                    st.info(f"ℹ️ Skipped: {', '.join(f'`{s}`' for s in skipped_specs)}")

                # ==============================================================
                # STEP 4: For each category, fetch pages from ReadMe.
                # - Endpoint pages: skip — Mintlify auto-generates from spec.
                # - Non-endpoint pages: fetch body from ReadMe, stage as MDX.
                # ==============================================================
                version_groups = []

                for category in categories:
                    cat_title = category.get("title", "")

                    pages, err = get_category_pages(readme_version, cat_title, readme_key)
                    if err:
                        st.warning(f"⚠️ Could not fetch pages for `{cat_title}`: {err}")
                        continue
                    if not pages:
                        continue

                    nav_pages     = []
                    cat_spec_path = None

                    for page in pages:
                        page_title = page.get("title", "")
                        page_slug  = page.get("slug",  "")

                        # Determine if endpoint page via operationId match
                        is_endpoint     = False
                        normalized_slug = re.sub(r"-\d+$", "", page_slug.lower())

                        for slug, paths in spec_path_index.items():
                            for path, methods in paths.items():
                                for method, op in methods.items():
                                    if not isinstance(op, dict):
                                        continue
                                    op_id = op.get("operationId", "")
                                    if op_id.lower() == page_slug.lower() or op_id.lower() == normalized_slug:
                                        is_endpoint   = True
                                        cat_spec_path = committed_specs[slug]
                                        break
                                if is_endpoint:
                                    break
                            if is_endpoint:
                                break

                        if is_endpoint:
                            continue  # Mintlify auto-generates endpoint pages from spec

                        # Non-endpoint — fetch body from ReadMe, stage MDX
                        detail = get_reference_page(readme_version, page_slug, readme_key)
                        body   = ""
                        if detail:
                            body = (detail.get("content") or {}).get("body") or ""
                        mdx_content   = build_content_mdx(page_title, body)
                        mdx_filename  = slug_to_mdx_filename(page_slug)
                        mdx_repo_path = f"{API_REF_BASE}/{display_version}/{mdx_filename}"
                        mdx_nav_path  = f"/api-reference/{display_version}/{mdx_filename[:-4]}"

                        all_files.append({"path": mdx_repo_path, "content": mdx_content})
                        nav_pages.append(mdx_nav_path)

                    group_entry = {"group": cat_title}
                    if cat_spec_path:
                        group_entry["openapi"] = cat_spec_path
                    if nav_pages:
                        group_entry["pages"] = nav_pages
                    version_groups.append(group_entry)

                st.write(f"📝 Prepared **{len([f for f in all_files if f['path'].endswith('.mdx')])}** content MDX file(s)")
                st.success(f"✅ Processed **{len(version_groups)}** categories")

                all_version_dropdowns.append({
                    "dropdown": display_version,
                    "groups":   version_groups,
                })

            # ==============================================================
            # STEP 5: Batch commit all spec YAML + content MDX files,
            # then patch docs.json in a second commit.
            # Two commits total = two Mintlify builds (not hundreds).
            # ==============================================================
            st.markdown("---\n#### 📝 Committing all files and patching `docs.json`")

            if all_files:
                with st.spinner(f"⬆️ Batch committing {len(all_files)} file(s) in one commit..."):
                    ok, err = batch_commit_files(
                        repo    = mintlify_repo,
                        token   = git_token,
                        branch  = MINTLIFY_BRANCH,
                        files   = all_files,
                        message = "🤖 Migrate API reference: " + ", ".join(VERSION_MAP[v] for v in selected_versions),
                    )
                if ok:
                    st.success(f"✅ Committed {len(all_files)} file(s) in one batch commit")
                else:
                    st.error(f"❌ Batch commit failed: {err}")
                    any_failures = True

            # Patch docs.json in a separate commit
            docs_url  = f"https://api.github.com/repos/{mintlify_repo}/contents/{DOCS_JSON_PATH}"
            docs_resp = gh_get(docs_url, git_token, params={"ref": MINTLIFY_BRANCH})

            if docs_resp.status_code != 200:
                st.error(f"❌ Could not fetch `docs.json`: {docs_resp.json().get('message', '')}")
            else:
                docs_data = json.loads(base64.b64decode(docs_resp.json()["content"]))
                patched   = False

                # Try tabs structure (elena/testNavigationChanges schema)
                for tab in docs_data.get("navigation", {}).get("tabs", []):
                    if tab.get("tab") == "API Reference":
                        for key in ["groups", "pages", "versions", "dropdowns"]:
                            tab.pop(key, None)
                        tab["dropdowns"] = all_version_dropdowns
                        patched = True
                        break

                # Fall back to products structure (main branch schema)
                if not patched:
                    for product in docs_data.get("navigation", {}).get("products", []):
                        if product.get("product") == "API Reference":
                            product["groups"] = all_version_dropdowns
                            patched = True
                            break

                if not patched:
                    st.error("❌ Could not find `API Reference` in `docs.json`.")
                else:
                    ok, put_resp = commit_file_to_branch(
                        repo          = mintlify_repo,
                        token         = git_token,
                        branch        = MINTLIFY_BRANCH,
                        file_path     = DOCS_JSON_PATH,
                        content_bytes = json.dumps(docs_data, indent=2).encode("utf-8"),
                        message       = "🤖 Update docs.json API Reference for: " + ", ".join(VERSION_MAP[v] for v in selected_versions),
                    )
                    if ok:
                        if any_failures:
                            st.warning("⚠️ Migration complete with some errors. Review above before merging.")
                        else:
                            st.success(
                                f"🎉 Migration complete! All files committed to `{MINTLIFY_BRANCH}` "
                                "in 2 commits. Mintlify will auto-deploy the branch preview shortly."
                            )
                    else:
                        st.error(f"❌ Failed to update `docs.json`: {put_resp.json().get('message', put_resp.text)}")

            shutil.rmtree("./mintlify_scratch", ignore_errors=True)


if __name__ == "__main__":
    main()
