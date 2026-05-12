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
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# JSON ENCODER — handles datetime objects that yaml.safe_load() produces
# ---------------------------------------------------------------------------

class SafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)

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
    payload = {"message": "🤖 Auto-update: Added new API slug mapping", "content": encoded, "branch": "main"}
    if sha:
        payload["sha"] = sha
    resp = gh_put(url, token, payload)
    return resp.status_code in [200, 201]

def commit_file_to_branch(repo, token, branch, file_path, content_bytes, message, retries=2):
    """Creates or updates a file on a GitHub branch. Retries on SHA conflict. Returns (ok, response)."""
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
        # 409 Conflict = SHA mismatch, retry by re-fetching SHA
        if resp.status_code == 409 and attempt < retries:
            continue
        return False, resp
    return False, resp

# ---------------------------------------------------------------------------
# README API v2 HELPERS
# ---------------------------------------------------------------------------

def readme_branch(readme_version):
    """
    Strips the leading 'v' from a ReadMe version slug for use in API calls.
    The ReadMe UI displays 'v2026.5.0-0' but the API expects '2026.5.0-0'.
    """
    return readme_version.lstrip("v")

def readme_get(path, readme_key, params=None):
    """GET against the ReadMe v2 API."""
    return requests.get(
        f"https://api.readme.com/v2{path}",
        headers={"Authorization": f"Bearer {readme_key}"},
        params=params
    )

def get_branch_api_slugs(readme_version, readme_key):
    """
    Returns the set of spec filenames (e.g. 'alation-agent-api.json')
    that exist for the given branch, by calling GET /branches/{branch}/apis.
    """
    resp = readme_get(f"/branches/{readme_branch(readme_version)}/apis", readme_key)
    if resp.status_code != 200:
        return set(), resp.text
    items = resp.json().get("data", [])
    return {item["filename"] for item in items}, None

def get_branch_reference_categories(readme_version, readme_key):
    """
    Returns ordered list of reference categories for the branch.
    Each category has: title, uri, position.
    Calls GET /branches/{branch}/categories/reference
    """
    resp = readme_get(f"/branches/{readme_branch(readme_version)}/categories/reference", readme_key)
    if resp.status_code != 200:
        return [], resp.text
    categories = resp.json().get("data", [])
    return sorted(categories, key=lambda c: c.get("position", 0)), None

def get_reference_page(readme_version, page_slug, readme_key):
    """
    Returns full detail for a single reference page including:
    - api.method, api.path  (HTTP method and endpoint path)
    - api.schema.info.title (owning spec title for matching)
    Calls GET /branches/{branch}/reference/{slug}
    """
    resp = readme_get(
        f"/branches/{readme_branch(readme_version)}/reference/{page_slug}",
        readme_key
    )
    if resp.status_code != 200:
        return None
    return resp.json().get("data", {})

def get_category_pages(readme_version, category_title, readme_key):
    """
    Returns ordered list of pages for a reference category.
    Each page has: title, slug, position, type.
    Calls GET /branches/{branch}/categories/reference/{title}/pages
    """
    resp = readme_get(
        f"/branches/{readme_branch(readme_version)}/categories/reference/{category_title}/pages",
        readme_key
    )
    if resp.status_code != 200:
        return [], resp.text
    pages = resp.json().get("data", [])
    return sorted(pages, key=lambda p: p.get("position", 0)), None

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
# OPENAPI FILE PREP (for ReadMe upload)
# ---------------------------------------------------------------------------

def prep_spec_content(filepath, version, readme_slug):
    """
    Loads a YAML spec, applies ReadMe/Mintlify prep transformations,
    and returns the result as JSON bytes. No temp files written.
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

    return json.dumps(data, indent=2, cls=SafeEncoder).encode("utf-8")

# ---------------------------------------------------------------------------
# MDX GENERATION HELPERS
# ---------------------------------------------------------------------------

def slug_to_mdx_filename(slug):
    """Converts a ReadMe page slug to a safe MDX filename."""
    return re.sub(r"[^a-z0-9-]", "-", slug.lower()).strip("-") + ".mdx"

def build_endpoint_mdx(page_title, page_slug, spec_rel_path, method, api_path):
    """
    Builds an MDX file for an individual API endpoint page.
    Uses the ReadMe page title and slug exactly as they appear in ReadMe.
    The openapi frontmatter field tells Mintlify which spec + endpoint to render.
    """
    frontmatter = f"""---
title: "{page_title}"
openapi: "{spec_rel_path} {method.upper()} {api_path}"
---
"""
    return frontmatter.encode("utf-8")

def build_overview_mdx(page_title, page_slug):
    """
    Builds an MDX file for non-endpoint reference pages
    (e.g. overview, authentication pages within a category).
    """
    frontmatter = f"""---
title: "{page_title}"
---
"""
    return frontmatter.encode("utf-8")

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

    # Build reverse mapping: readme_slug → list of possible eng_keys
    # e.g. "cde" → ["cde-public-api-fixed", "cde-public-api"]
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
            "1. Pull the spec list and category structure from ReadMe v2 API\n"
            "2. Source spec content from the engineering repo YAML\n"
            "3. Commit spec JSON files to the Mintlify branch\n"
            "4. Generate MDX pages using ReadMe titles and slugs\n"
            "5. Patch `docs.json` to mirror ReadMe's navigation exactly"
        )
        st.caption(f"🎯 Target: `{mintlify_repo}` → `{MINTLIFY_BRANCH}`")

        selected_versions = st.multiselect(
            "Select ReadMe versions to migrate",
            options=list(VERSION_MAP.keys()),
            format_func=lambda v: f"{VERSION_MAP[v]}  ({v})",
            default=["v2026.5.0-0"],
            help="ReadMe branch slug → canonical display version"
        )

        # --- Debug expander: inspect raw ReadMe API responses ---
        with st.expander("🔬 Debug: Inspect ReadMe API responses"):
            st.caption(
                "Use this to inspect the raw API response from ReadMe before running "
                "the full migration. Helps verify what fields are available on reference pages."
            )
            debug_version = st.selectbox(
                "Version to inspect",
                options=list(VERSION_MAP.keys()),
                format_func=lambda v: f"{VERSION_MAP[v]}  ({v})",
                key="debug_version"
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
                        readme_key
                    )
                    st.write(f"**Status:** {resp.status_code}")
                    st.json(resp.json())

            # Also let them inspect a single reference page by slug
            debug_slug = st.text_input("Reference page slug to inspect", key="debug_slug")
            if st.button("🔍 Inspect Single Reference Page") and debug_slug:
                resp = readme_get(
                    f"/branches/{readme_branch(debug_version)}/reference/{debug_slug}",
                    readme_key
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

            all_version_dropdowns = []  # final docs.json structure
            any_failures          = False

            for readme_version in selected_versions:
                display_version = VERSION_MAP[readme_version]
                st.markdown(f"---\n#### 📦 `{display_version}` (ReadMe: `{readme_version}`)")

                # ==============================================================
                # STEP 1: Get the list of spec slugs for this version from ReadMe
                # ==============================================================
                with st.spinner(f"Fetching spec list for `{readme_version}`..."):
                    branch_slugs, err = get_branch_api_slugs(readme_version, readme_key)
                if err:
                    st.error(f"❌ Failed to fetch spec list: {err}")
                    any_failures = True
                    continue
                st.write(f"📋 Found **{len(branch_slugs)}** spec(s) in ReadMe for this version")

                # ==============================================================
                # STEP 2: Get reference categories (ordered) from ReadMe
                # ==============================================================
                with st.spinner(f"Fetching reference categories for `{readme_version}`..."):
                    categories, err = get_branch_reference_categories(readme_version, readme_key)
                if err:
                    st.error(f"❌ Failed to fetch categories: {err}")
                    any_failures = True
                    continue
                st.write(f"📂 Found **{len(categories)}** reference categories")

                # ==============================================================
                # STEP 3: For each spec ReadMe has for this version, use
                # slug_mapping.json to find the exact YAML in the eng repo.
                #
                # Flow:
                #   ReadMe filename → readme_slug
                #   slug_mapping (reversed) → eng_key
                #   eng repo → {eng_key}.yaml
                # ==============================================================
                committed_specs = {}  # readme_slug → repo-relative path
                skipped_specs   = []
                failed_specs    = []

                for filename in sorted(branch_slugs):
                    # Strip extension to get the ReadMe slug
                    # e.g. "alation-agent-api.json" → "alation-agent-api"
                    readme_slug = re.sub(r"\.(json|yaml|yml)$", "", filename)

                    # Look up the eng_key using slug_mapping (reversed)
                    # e.g. "alation-agent-api" → ["agent"]
                    eng_keys = reverse_mapping.get(readme_slug)
                    if not eng_keys:
                        skipped_specs.append(readme_slug)
                        st.warning(f"⚠️ `{readme_slug}`: not in slug_mapping.json — skipping.")
                        continue

                    # Find the YAML in the pulled eng repo using the eng_key
                    # slug_mapping guarantees the filename: eng_key + ".yaml"
                    spec_content = None
                    used_eng_key = None
                    for eng_key in eng_keys:
                        for spec_dir in [path_main, path_logical]:
                            candidate = workspace_dir / spec_dir / f"{eng_key}.yaml"
                            if candidate.exists():
                                try:
                                    spec_content = prep_spec_content(candidate, display_version, readme_slug)
                                    used_eng_key = eng_key
                                except Exception as e:
                                    st.error(f"❌ `{readme_slug}`: failed to prep `{eng_key}.yaml` — {e}")
                                break  # found the file, stop searching dirs
                        if spec_content is not None:
                            break  # found a working eng_key, stop trying others

                    if spec_content is None:
                        # The YAML should exist since slug_mapping.json defines it —
                        # if it's missing the eng repo may need to be re-pulled.
                        skipped_specs.append(readme_slug)
                        tried = ", ".join(f"`{k}.yaml`" for k in eng_keys)
                        st.error(
                            f"❌ `{readme_slug}`: YAML not found in pulled eng repo "
                            f"(tried {tried}). Re-pull specs and try again."
                        )
                        continue

                    # Commit the prepped spec JSON to the Mintlify branch
                    spec_repo_path = f"{API_REF_BASE}/{display_version}/{readme_slug}.json"
                    ok, put_resp   = commit_file_to_branch(
                        repo          = mintlify_repo,
                        token         = git_token,
                        branch        = MINTLIFY_BRANCH,
                        file_path     = spec_repo_path,
                        content_bytes = spec_content,
                        message       = f"🤖 Spec: {readme_slug} ({display_version}) from {used_eng_key}.yaml"
                    )
                    if ok:
                        committed_specs[readme_slug] = f"api-reference/{display_version}/{readme_slug}.json"
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
                # STEP 4: Build a local index of all spec paths, then for each
                # category fetch its pages from ReadMe, look up method + path
                # from the local spec index, generate MDX, and commit.
                # This avoids one ReadMe API call per page (hundreds of calls).
                # ==============================================================

                # Build index: {readme_slug: {"/path/": {"post": True, ...}}}
                # by reading the committed spec JSON files via GitHub API
                spec_path_index = {}
                for readme_slug, rel_path in committed_specs.items():
                    spec_url = (
                        f"https://api.github.com/repos/{mintlify_repo}"
                        f"/contents/{API_REF_BASE}/{display_version}/{readme_slug}.json"
                    )
                    spec_resp = gh_get(spec_url, git_token, params={"ref": MINTLIFY_BRANCH})
                    if spec_resp.status_code == 200:
                        try:
                            spec_data = json.loads(
                                base64.b64decode(spec_resp.json()["content"])
                            )
                            spec_path_index[readme_slug] = spec_data.get("paths", {})
                        except Exception:
                            spec_path_index[readme_slug] = {}

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
                        page_slug  = page.get("slug", "")

                        # Look up this page's endpoint in the local spec index.
                        # ReadMe uses operationId as the page slug, just lowercased.
                        # e.g. operationId "createAPIAccessToken" → slug "createapiaccesstoken"
                        api_method    = ""
                        api_path      = ""
                        spec_rel_path = None

                        for readme_slug, paths in spec_path_index.items():
                            for path, methods in paths.items():
                                for method in methods.keys():
                                    op = methods[method]
                                    if not isinstance(op, dict):
                                        continue
                                    op_id = op.get("operationId", "")
                                    if op_id.lower() == page_slug.lower():
                                        api_method    = method
                                        api_path      = path
                                        spec_rel_path = committed_specs[readme_slug]
                                        break
                                if api_path:
                                    break
                            if api_path:
                                break

                        mdx_filename  = slug_to_mdx_filename(page_slug)
                        mdx_repo_path = f"{API_REF_BASE}/{display_version}/{mdx_filename}"
                        mdx_nav_path  = f"api-reference/{display_version}/{page_slug}"

                        if api_method and api_path and spec_rel_path:
                            if cat_spec_path is None:
                                cat_spec_path = spec_rel_path
                            mdx_content = build_endpoint_mdx(
                                page_title, page_slug, spec_rel_path, api_method, api_path
                            )
                        else:
                            mdx_content = build_overview_mdx(page_title, page_slug)

                        ok, _ = commit_file_to_branch(
                            repo          = mintlify_repo,
                            token         = git_token,
                            branch        = MINTLIFY_BRANCH,
                            file_path     = mdx_repo_path,
                            content_bytes = mdx_content,
                            message       = f"🤖 MDX: {page_slug} ({display_version})"
                        )
                        if ok:
                            nav_pages.append(mdx_nav_path)

                    if nav_pages:
                        # Determine the dominant spec for this category
                        # by finding which spec was most referenced by pages in it
                        group_entry = {
                            "group": cat_title,
                            "pages": nav_pages
                        }
                        # Add openapi field pointing to the owning spec if we can determine it
                        if cat_spec_path:
                            group_entry["openapi"] = cat_spec_path
                        version_groups.append(group_entry)

                st.success(f"✅ Generated MDX pages for **{len(version_groups)}** categories")

                all_version_dropdowns.append({
                    "dropdown": display_version,
                    "groups":   version_groups
                })

            # ==================================================================
            # STEP 5: Patch docs.json with the full versioned structure
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
                        )
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
