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

def commit_file_to_branch(repo, token, branch, file_path, content_bytes, message, retries=3):
    """Creates or updates a file on a GitHub branch. Retries on SHA conflict (409 or 422)."""
    url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
    for attempt in range(retries + 1):
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
        if resp.status_code in [200, 201]:
            return True, resp
        # Retry on SHA conflict (409) or SHA mismatch (422)
        if resp.status_code in [409, 422] and attempt < retries:
            continue
        return False, resp
    return False, resp

# ---------------------------------------------------------------------------
# README API v2 HELPERS
# ---------------------------------------------------------------------------

def readme_branch(readme_version):
    """Strip leading 'v' — ReadMe UI shows 'v2026.5.0-0', API expects '2026.5.0-0'."""
    return readme_version.lstrip("v")

def readme_get(path, readme_key, params=None):
    """GET against the ReadMe v2 API."""
    return requests.get(
        f"https://api.readme.com/v2{path}",
        headers={"Authorization": f"Bearer {readme_key}"},
        params=params,
    )

def get_branch_api_slugs(readme_version, readme_key):
    """Returns set of spec filenames for this branch via GET /branches/{branch}/apis."""
    resp = readme_get(f"/branches/{readme_branch(readme_version)}/apis", readme_key)
    if resp.status_code != 200:
        return set(), resp.text
    items = resp.json().get("data", [])
    return {item["filename"] for item in items}, None

def get_branch_reference_categories(readme_version, readme_key):
    """Returns ordered list of reference categories via GET /branches/{branch}/categories/reference."""
    resp = readme_get(f"/branches/{readme_branch(readme_version)}/categories/reference", readme_key)
    if resp.status_code != 200:
        return [], resp.text
    cats = resp.json().get("data", [])
    return sorted(cats, key=lambda c: c.get("position", 0)), None

def get_category_pages(readme_version, category_title, readme_key):
    """Returns ordered pages for a category via GET /branches/{branch}/categories/reference/{title}/pages."""
    resp = readme_get(
        f"/branches/{readme_branch(readme_version)}/categories/reference/{category_title}/pages",
        readme_key,
    )
    if resp.status_code != 200:
        return [], resp.text
    pages = resp.json().get("data", [])
    return sorted(pages, key=lambda p: p.get("position", 0)), None

def get_reference_page(readme_version, page_slug, readme_key):
    """
    Returns full detail for a single reference page via GET /branches/{branch}/reference/{slug}.
    Used to fetch Markdown body content for non-endpoint (overview) pages.
    """
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
    run_env        = os.environ.copy()
    run_env["CI"]  = "true"
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
# Used by Tab 1 (Git Repo Pipeline) and Tab 2 (Manual Upload) for ReadMe uploads.
# Also used internally by prep_spec_content for the Mintlify migration.
# ---------------------------------------------------------------------------

def prep_openapi_file(filepath, version, target_slug):
    """Writes a prepped YAML file next to the source for CLI validation/upload."""
    with open(filepath, "r") as f:
        data = yaml.safe_load(f)
    if "info" not in data:
        data["info"] = {}
    data["info"]["version"] = version
    if "x-readme" not in data:
        data["x-readme"] = {}
    data["x-readme"]["explorer-enabled"] = False
    data["x-readme"]["proxy-enabled"]    = True
    if "servers" in data and isinstance(data["servers"], list):
        for server in data["servers"]:
            if "variables" in server:
                if "protocol" in server["variables"]:
                    server["variables"]["protocol"]["default"] = "https"
                if "base-url" in server["variables"]:
                    server["variables"]["base-url"]["default"] = "alation_domain"
    yaml_filepath = filepath.parent / f"{target_slug}_prepped.yaml"
    with open(yaml_filepath, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    return yaml_filepath

def prep_spec_content(filepath, version, readme_slug):
    """
    Loads a YAML spec, applies prep transformations in memory,
    and returns YAML bytes. No temp files written.
    Used by the Mintlify migration (Tab 3).
    """
    with open(filepath, "r") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"YAML did not parse to a dict — got {type(data)}")

    data.setdefault("info", {})["version"] = version
    data.setdefault("x-readme", {}).update({
        "explorer-enabled": False,
        "proxy-enabled":    True,
    })
    for server in data.get("servers", []):
        variables = server.get("variables", {})
        if "protocol" in variables:
            variables["protocol"]["default"] = "https"
        if "base-url" in variables:
            variables["base-url"]["default"] = "alation_domain"

    return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True).encode("utf-8")

# ---------------------------------------------------------------------------
# MDX BUILDERS
# ---------------------------------------------------------------------------

def slug_to_mdx_filename(slug):
    """Converts a ReadMe page slug to a safe MDX filename."""
    return re.sub(r"[^a-z0-9-]", "-", slug.lower()).strip("-") + ".mdx"

def build_endpoint_mdx(page_title, spec_rel_path, method, api_path):
    """
    MDX for an API endpoint page.
    The openapi frontmatter tells Mintlify which spec + endpoint to render.
    """
    # Escape any double quotes in the title
    safe_title = page_title.replace('"', '\\"')
    return (
        f'---\n'
        f'title: "{safe_title}"\n'
        f'openapi: "{spec_rel_path} {method.upper()} {api_path}"\n'
        f'---\n'
    ).encode("utf-8")

def build_content_mdx(page_title, body):
    """
    MDX for a non-endpoint page (overview, authentication, custom content).
    Includes the Markdown body pulled from ReadMe so the page is not empty.
    """
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

    # Build reverse mapping: readme_slug → [eng_key, ...]
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
            parsed.scheme,
            f"{git_user}:{git_token}@{parsed.netloc}",
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
                yaml_files.extend(
                    f for f in tp.glob("*.yaml") if not f.name.endswith("_prepped.yaml")
                )

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
        st.subheader("🌿 Migrate ReadMe → Mintlify Branch")
        st.info(
            "For each selected version this tab will:\n"
            "1. Pull spec list and category structure from ReadMe v2 API\n"
            "2. Source spec content from the engineering repo YAML\n"
            "3. Commit spec YAML files to the Mintlify branch\n"
            "4. Generate content MDX pages for overview/guide pages with Markdown body from ReadMe\n"
            "5. Patch `docs.json` — endpoint pages are auto-generated by Mintlify from the spec"
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

            for readme_version in selected_versions:
                display_version = VERSION_MAP[readme_version]
                st.markdown(f"---\n#### 📦 `{display_version}` (ReadMe: `{readme_version}`)")

                # ==============================================================
                # STEP 1: Get spec list from ReadMe for this version
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
                # STEP 2.5: Clean up stale .json spec files left from previous
                # runs before we switched to .yaml. List all files in the
                # version directory and delete any .json spec files.
                # ==============================================================
                with st.spinner("🧹 Cleaning up stale spec files from previous runs..."):
                    list_url  = f"https://api.github.com/repos/{mintlify_repo}/contents/{API_REF_BASE}/{display_version}"
                    list_resp = gh_get(list_url, git_token, params={"ref": MINTLIFY_BRANCH})
                    if list_resp.status_code == 200:
                        stale_deleted = 0
                        for item in list_resp.json():
                            name = item.get("name", "")
                            # Delete old .json spec files (not docs.json)
                            if name.endswith(".json") and item.get("type") == "file":
                                del_resp = requests.delete(
                                    f"https://api.github.com/repos/{mintlify_repo}/contents/{API_REF_BASE}/{display_version}/{name}",
                                    headers={"Authorization": f"token {git_token}", "Accept": "application/vnd.github.v3+json"},
                                    json={"message": f"🧹 Remove stale spec file: {name}", "sha": item["sha"], "branch": MINTLIFY_BRANCH}
                                )
                                if del_resp.status_code in [200, 201]:
                                    stale_deleted += 1
                        if stale_deleted:
                            st.info(f"🧹 Removed {stale_deleted} stale `.json` spec file(s)")

                # ==============================================================
                # STEP 3: For each spec ReadMe has for this version, find the
                # YAML in the eng repo using slug_mapping.json, prep it, and
                # commit as YAML to the Mintlify branch.
                #
                # Flow:
                #   ReadMe filename  →  readme_slug  (strip extension)
                #   slug_mapping rev →  eng_key      (e.g. "agent")
                #   eng repo         →  {eng_key}.yaml
                # ==============================================================
                committed_specs = {}   # readme_slug → "api-reference/{ver}/{slug}.yaml"
                spec_path_index = {}   # readme_slug → {"/path/": {"method": op_data, ...}}
                skipped_specs   = []
                failed_specs    = []

                for filename in sorted(branch_slugs):
                    readme_slug = re.sub(r"\.(json|yaml|yml)$", "", filename)
                    eng_keys    = reverse_mapping.get(readme_slug)

                    spec_content = None
                    used_source  = None

                    # --- Source 1: Engineering repo YAML ---
                    if eng_keys:
                        for eng_key in eng_keys:
                            for spec_dir in [path_main, path_logical]:
                                candidate = workspace_dir / spec_dir / f"{eng_key}.yaml"
                                if candidate.exists():
                                    try:
                                        spec_content = prep_spec_content(candidate, display_version, readme_slug)
                                        used_source  = f"{eng_key}.yaml"
                                    except Exception as e:
                                        st.error(f"❌ `{readme_slug}`: failed to prep `{eng_key}.yaml` — {e}")
                                        spec_content = None
                                    break
                            if spec_content is not None:
                                break

                        if spec_content is None:
                            tried = ", ".join(f"`{k}.yaml`" for k in eng_keys)
                            st.warning(f"⚠️ `{readme_slug}`: YAML not found in eng repo (tried {tried}) — trying ReadMe...")

                    # --- Source 2: ReadMe API fallback ---
                    if spec_content is None:
                        try:
                            resp = requests.get(
                                f"https://api.readme.com/v2/branches/{readme_version.lstrip('v')}/apis/{readme_slug}.json",
                                headers={"Authorization": f"Bearer {readme_key}"},
                            )
                            if resp.status_code == 200:
                                data = resp.json()
                                if isinstance(data, dict):
                                    data.setdefault("info", {})["version"] = display_version
                                    data.setdefault("x-readme", {}).update({
                                        "explorer-enabled": False,
                                        "proxy-enabled":    True,
                                    })
                                    for server in data.get("servers", []):
                                        variables = server.get("variables", {})
                                        if "protocol" in variables:
                                            variables["protocol"]["default"] = "https"
                                        if "base-url" in variables:
                                            variables["base-url"]["default"] = "alation_domain"
                                    spec_content = yaml.dump(
                                        data,
                                        default_flow_style=False,
                                        sort_keys=False,
                                        allow_unicode=True,
                                    ).encode("utf-8")
                                    used_source = "ReadMe (manually uploaded)"
                        except Exception as e:
                            st.warning(f"⚠️ `{readme_slug}`: ReadMe fallback failed — {e}")

                    if spec_content is None:
                        skipped_specs.append(readme_slug)
                        if not eng_keys:
                            st.warning(f"⚠️ `{readme_slug}`: not in slug_mapping.json and not found in ReadMe — skipping.")
                        else:
                            st.error(f"❌ `{readme_slug}`: not found in eng repo or ReadMe — skipping.")
                        continue

                    spec_repo_path = f"{API_REF_BASE}/{display_version}/{readme_slug}.yaml"
                    ok, put_resp   = commit_file_to_branch(
                        repo          = mintlify_repo,
                        token         = git_token,
                        branch        = MINTLIFY_BRANCH,
                        file_path     = spec_repo_path,
                        content_bytes = spec_content,
                        message       = f"🤖 Spec: {readme_slug} ({display_version}) from {used_source}",
                    )
                    if ok:
                        committed_specs[readme_slug] = f"api-reference/{display_version}/{readme_slug}.yaml"
                        # Build path index in memory from spec content
                        try:
                            spec_path_index[readme_slug] = yaml.safe_load(spec_content).get("paths", {})
                        except Exception:
                            spec_path_index[readme_slug] = {}
                    else:
                        failed_specs.append(readme_slug)
                        any_failures = True
                        st.error(f"❌ Failed to commit `{readme_slug}`: {put_resp.json().get('message', put_resp.text)}")

                st.success(f"✅ Committed **{len(committed_specs)}** spec(s)")
                if skipped_specs:
                    st.info(f"ℹ️ Skipped: {', '.join(f'`{s}`' for s in skipped_specs)}")
                if failed_specs:
                    st.warning(f"⚠️ Failed: {', '.join(f'`{s}`' for s in failed_specs)}")

                # ==============================================================
                # STEP 5: For each category, fetch pages from ReadMe.
                # - Non-endpoint pages (overview, content): fetch Markdown body
                #   from ReadMe, write MDX, add to nav pages list.
                # - Endpoint pages: skip MDX generation entirely — Mintlify
                #   auto-generates these from the spec via the group openapi field.
                # The group-level openapi field in docs.json is what drives
                # endpoint rendering, not individual MDX files.
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

                        # Determine if this is an endpoint page by checking
                        # if it matches any operationId in the spec index
                        is_endpoint = False
                        for readme_slug, paths in spec_path_index.items():
                            for path, methods in paths.items():
                                for method, op in methods.items():
                                    if not isinstance(op, dict):
                                        continue
                                    op_id = op.get("operationId", "")
                                    normalized_slug = re.sub(r"-\d+$", "", page_slug.lower())
                                    if op_id.lower() == page_slug.lower() or op_id.lower() == normalized_slug:
                                        is_endpoint   = True
                                        cat_spec_path = committed_specs[readme_slug]
                                        break
                                if is_endpoint:
                                    break
                            if is_endpoint:
                                break

                        if is_endpoint:
                            # Skip — Mintlify auto-generates endpoint pages from spec
                            continue

                        # Non-endpoint page — fetch Markdown body from ReadMe
                        # and write a content MDX file
                        detail = get_reference_page(readme_version, page_slug, readme_key)
                        body   = ""
                        if detail:
                            body = (detail.get("content") or {}).get("body") or ""
                        mdx_content   = build_content_mdx(page_title, body)
                        mdx_filename  = slug_to_mdx_filename(page_slug)
                        mdx_repo_path = f"{API_REF_BASE}/{display_version}/{mdx_filename}"
                        mdx_nav_path  = f"api-reference/{display_version}/{mdx_filename[:-4]}"

                        ok, _ = commit_file_to_branch(
                            repo          = mintlify_repo,
                            token         = git_token,
                            branch        = MINTLIFY_BRANCH,
                            file_path     = mdx_repo_path,
                            content_bytes = mdx_content,
                            message       = f"🤖 Content MDX: {page_slug} ({display_version})",
                        )
                        if ok:
                            nav_pages.append(mdx_nav_path)

                    # Build group entry — openapi field drives endpoint auto-generation
                    # pages array only contains non-endpoint content pages
                    group_entry = {"group": cat_title}
                    if cat_spec_path:
                        group_entry["openapi"] = cat_spec_path
                    if nav_pages:
                        group_entry["pages"] = nav_pages
                    version_groups.append(group_entry)

                st.success(f"✅ Generated content MDX for **{len(version_groups)}** categories (endpoints auto-generated by Mintlify)")

                all_version_dropdowns.append({
                    "dropdown": display_version,
                    "groups":   version_groups,
                })

            # ==================================================================
            # STEP 6: Patch docs.json with the full versioned navigation
            # ==================================================================
            st.markdown("---\n#### 📝 Patching `docs.json`")

            docs_url  = f"https://api.github.com/repos/{mintlify_repo}/contents/{DOCS_JSON_PATH}"
            docs_resp = gh_get(docs_url, git_token, params={"ref": MINTLIFY_BRANCH})

            if docs_resp.status_code != 200:
                st.error(f"❌ Could not fetch `docs.json`: {docs_resp.json().get('message', '')}")
            else:
                docs_data = json.loads(base64.b64decode(docs_resp.json()["content"]))

                patched = False
                for tab in docs_data.get("navigation", {}).get("tabs", []):
                    if tab.get("tab") == "API Reference":
                        for key in ["groups", "pages", "versions", "dropdowns"]:
                            tab.pop(key, None)
                        tab["dropdowns"] = all_version_dropdowns
                        patched = True
                        break

                if not patched:
                    st.error("❌ Could not find `API Reference` tab in `docs.json`.")
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
                        ),
                    )
                    if ok:
                        if any_failures:
                            st.warning("⚠️ `docs.json` updated but some specs had errors. Review above before merging.")
                        else:
                            st.success(
                                f"🎉 Migration complete! `docs.json` committed to `{MINTLIFY_BRANCH}`. "
                                "Mintlify will auto-deploy the branch preview shortly."
                            )
                    else:
                        st.error(f"❌ Failed to update `docs.json`: {put_resp.json().get('message', put_resp.text)}")

            shutil.rmtree("./mintlify_scratch", ignore_errors=True)


if __name__ == "__main__":
    main()
