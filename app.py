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

# --- PAGE CONFIG ---
st.set_page_config(page_title="Alation OpenAPI Manager", page_icon="📘", layout="wide")

# --- GITHUB API HELPER FUNCTIONS (SERVICE ACCOUNT) ---
def load_slug_mapping(repo_name, svc_token):
    """Fetches the current slug_mapping.json from the app's GitHub repo."""
    url = f"https://api.github.com/repos/{repo_name}/contents/slug_mapping.json"
    headers = {"Authorization": f"token {svc_token}", "Accept": "application/vnd.github.v3+json"}
    
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        data = response.json()
        content = base64.b64decode(data['content']).decode('utf-8')
        return json.loads(content), data['sha']
    elif response.status_code == 404:
        # File doesn't exist yet, return empty dict
        return {}, None
    else:
        st.error(f"⚠️ Failed to load slug mapping: {response.text}")
        return {}, None

def save_slug_mapping(repo_name, svc_token, updated_mapping, sha):
    """Commits the updated slug_mapping.json back to the GitHub repo."""
    url = f"https://api.github.com/repos/{repo_name}/contents/slug_mapping.json"
    headers = {"Authorization": f"token {svc_token}", "Accept": "application/vnd.github.v3+json"}
    
    json_str = json.dumps(updated_mapping, indent=4)
    encoded_content = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
    
    payload = {
        "message": "🤖 Auto-update: Added new API slug mapping",
        "content": encoded_content,
        "branch": "main" # Update to "master" if your app repo uses master
    }
    if sha:
        payload["sha"] = sha
        
    response = requests.put(url, headers=headers, json=payload)
    return response.status_code in [200, 201]

# --- NODE.JS SETUP ---
def ensure_node_installed():
    node_version = "v20.11.0"
    install_dir = Path("./node_runtime")
    node_dirname = f"node-{node_version}-linux-x64"
    node_bin_path = install_dir / node_dirname / "bin"
    
    try:
        if subprocess.run(["node", "-v"], capture_output=True).returncode == 0: return
    except FileNotFoundError: pass

    if not node_bin_path.exists():
        with st.spinner("🔧 Initializing environment (Node.js)..."):
            url = f"https://nodejs.org/dist/{node_version}/{node_dirname}.tar.xz"
            resp = requests.get(url, stream=True)
            tar_path = Path("node.tar.xz")
            with open(tar_path, 'wb') as f: f.write(resp.raw.read())
            with tarfile.open(tar_path) as tar: tar.extractall(install_dir)
            os.remove(tar_path)
    
    os.environ["PATH"] = f"{str(node_bin_path.absolute())}{os.pathsep}{os.environ['PATH']}"

# --- COMMAND RUNNER ---
def run_command_ui(cmd_string, cwd=None, mask_secrets=[]):
    display_cmd = cmd_string
    for s in mask_secrets:
        if s: display_cmd = display_cmd.replace(s, "***")
    
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
        env=run_env
    )
    
    for line in process.stdout:
        clean_line = line.strip()
        for s in mask_secrets:
            if s: clean_line = clean_line.replace(s, "***")
        st.text(clean_line)
        
    process.wait()
    return process.returncode

def prep_openapi_file(filepath, version, target_slug):
    with open(filepath, "r") as f: data = yaml.safe_load(f)
    if "info" not in data: data["info"] = {}
    data["info"]["version"] = version
    
    if "x-readme" not in data: data["x-readme"] = {}
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

# --- MAIN APP ---
def main():
    ensure_node_installed()
    st.title("📘 Alation OpenAPI Manager")
    
    # 1. Load Secrets
    readme_key = st.secrets.get("README_API_KEY", "")
    git_token = st.secrets.get("GIT_TOKEN", "")
    git_user = st.secrets.get("GIT_USER", "")
    eng_repo_url = st.secrets.get("ENG_REPO_URL", "")
    path_main = st.secrets.get("PATH_SPECS_MAIN", "django/static/swagger/specs")
    path_logical = st.secrets.get("PATH_SPECS_LOGICAL", "django/static/swagger/specs/logical_metadata")
    
    # New Service Account Secrets
    svc_git_token = st.secrets.get("SVC_GIT_TOKEN", "")
    app_repo_name = st.secrets.get("APP_REPO_NAME", "")
    
    workspace_dir = Path("./temp_eng_workspace")
    workspace_dir.mkdir(exist_ok=True)

    # 2. Fetch the latest Slug Mapping Database from GitHub
    current_mapping = {}
    current_sha = None
    if svc_git_token and app_repo_name:
        current_mapping, current_sha = load_slug_mapping(app_repo_name, svc_git_token)
    else:
        st.error("⚠️ Missing Service Account secrets! Cannot load or save slug mappings.")

    with st.sidebar:
        st.header("⚙️ Task Configuration")
        eng_branch = st.text_input("Engineering Branch", value="master")
        target_version = st.text_input("ReadMe Version", value="v2026.3.1-0") # Defaulting to live
        st.divider()
        st.caption(f"🔒 Eng Repo: `{eng_repo_url}`")
        st.caption(f"📂 App Repo: `{app_repo_name}`")

    # STEP 1: PULL (Using personal PAT)
    if st.button(f"📥 1. Pull Specs from `{eng_branch}`"):
        if workspace_dir.exists(): shutil.rmtree(workspace_dir)
        workspace_dir.mkdir()
        parsed = urllib.parse.urlparse(eng_repo_url)
        auth_url = urllib.parse.urlunparse((parsed.scheme, f"{git_user}:{git_token}@{parsed.netloc}", parsed.path, "", "", ""))
        with st.spinner("Cloning engineering repo..."):
            p = subprocess.run(["git", "clone", "--depth", "1", "--branch", eng_branch, auth_url, str(workspace_dir)], capture_output=True)
            if p.returncode == 0: st.success("✅ Specs pulled.")
            else: st.error(f"❌ Error: {p.stderr.decode()}")

    # --- WORKFLOW TABS ---
    st.divider()
    tab_git, tab_manual, tab_mintlify = st.tabs(["🐙 Git Repo Pipeline", "📂 Manual File Upload", "🌿 Pull to Mintlify"])
    npx = shutil.which("npx")

    # ==========================================
    # TAB 1: GIT REPO PIPELINE
    # ==========================================
    with tab_git:
        st.subheader("🛠️ 2. Select API Spec")
        yaml_files = []
        for p in [path_main, path_logical]:
            tp = workspace_dir / p
            if tp.exists(): 
                # Filter out the temporary prepped files
                valid_files = [f for f in tp.glob("*.yaml") if not f.name.endswith("_prepped.yaml")]
                yaml_files.extend(valid_files)
        
        file_options = sorted([f.name for f in yaml_files])
        
        # FIX: Instead of 'return', we gracefully show a message so the rest of the app still renders!
        if not file_options:
            st.info("👈 Please click '1. Pull Specs' above to load files from the repository.")
        else:
            selected_file_name = st.selectbox("Select Spec", file_options)
            selected_file_path = next(f for f in yaml_files if f.name == selected_file_name)
            
            # Check mapping dictionary
            mapped_id = current_mapping.get(selected_file_path.stem, "")
            is_new_file = False
            
            if not mapped_id:
                is_new_file = True
                try:
                    with open(selected_file_path, "r") as f: temp_data = yaml.safe_load(f)
                    raw_title = temp_data.get("info", {}).get("title", selected_file_path.stem)
                    mapped_id = re.sub(r'[^a-z0-9]+', '-', raw_title.lower()).strip('-')
                except Exception:
                    mapped_id = selected_file_path.stem
            
            col1, col2 = st.columns(2)
            col1.info(f"**Original File:** `{selected_file_name}`")
            if is_new_file: col2.warning(f"**Auto-Generated Slug:** `{mapped_id}`")
            elif mapped_id: col2.success(f"**Mapped Slug:** `{mapped_id}`")
                
            final_id = st.text_input("Target ReadMe Slug (Filename):", value=mapped_id)

            # STEP 3: ACTIONS
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
                            upload_cmd = f"{npx} --yes rdme openapi upload {prepped.name} --key {readme_key} --slug {final_id}.json --branch {target_version}"
                            
                            if run_command_ui(upload_cmd, cwd=abs_cwd, mask_secrets=[readme_key]) == 0:
                                st.success("🎉 Successfully uploaded to ReadMe!")
                                if is_new_file:
                                    with st.spinner("Pushing new slug to App repo..."):
                                        current_mapping[selected_file_path.stem] = final_id
                                        saved = save_slug_mapping(app_repo_name, svc_git_token, current_mapping, current_sha)
                                        if saved:
                                            st.success(f"📝 Added `'{selected_file_path.stem}': '{final_id}'` to `slug_mapping.json`.")
                                        else:
                                            st.warning("⚠️ Upload succeeded, but failed to save the mapping to GitHub.")
                            else:
                                st.error("❌ Upload failed. See logs above.")

    # ==========================================
    # TAB 2: MANUAL FILE OVERRIDE
    # ==========================================
    with tab_manual:
        st.subheader("📂 Manual File Override")
        st.info("Upload your modified YAML or JSON spec. **Note:** You must 'Pull Specs' first so the app has the external `$ref` dependency files to validate against!")
        
        # Ensure the repo is pulled so we have the dependencies
        if not list(workspace_dir.glob("**/*.yaml")):
            st.warning("⚠️ Please click '1. Pull Specs' in the sidebar first to load the dependency schemas.")
        else:
            manual_file = st.file_uploader("Upload your modified YAML or JSON spec", type=["yaml", "yml", "json"])
            
            if manual_file is not None:
                # Search the cloned repo to find where this file naturally lives
                target_paths = list(workspace_dir.rglob(manual_file.name))
                
                if not target_paths:
                    # SOLUTION: Handle independent files instead of throwing an error
                    st.info(f"ℹ️ `{manual_file.name}` not found in the repository. Treating as an independent, standalone file.")
                    
                    # Save it directly to the root of our temporary workspace
                    manual_path = workspace_dir / manual_file.name
                    with open(manual_path, "wb") as f:
                        f.write(manual_file.getbuffer())
                        
                else:
                    # We found the original file's location! 
                    manual_path = target_paths[0]
                    
                    # Overwrite the Git-pulled version with your custom uploaded version
                    with open(manual_path, "wb") as f:
                        f.write(manual_file.getbuffer())
                    
                    st.success(f"✅ Successfully injected your custom edits into `{manual_path.relative_to(workspace_dir)}`")
                
                # Auto-detect slug using your existing database logic
                manual_mapped_id = current_mapping.get(manual_path.stem, "")
                is_manual_new = False
                
                if not manual_mapped_id:
                    is_manual_new = True
                    try:
                        with open(manual_path, "r") as f: temp_data = yaml.safe_load(f)
                        raw_title = temp_data.get("info", {}).get("title", manual_path.stem)
                        manual_mapped_id = re.sub(r'[^a-z0-9]+', '-', raw_title.lower()).strip('-')
                    except Exception:
                        manual_mapped_id = manual_path.stem
                
                # Use a unique key for Streamlit so it doesn't clash with the Git tab's input box
                manual_final_id = st.text_input("Target ReadMe Slug (Manual):", value=manual_mapped_id, key="manual_slug_input")
                
                col_mv, col_mu = st.columns(2)
                with col_mv:
                    if st.button("🔍 Validate Custom Spec"):
                        manual_prepped = prep_openapi_file(manual_path, target_version, manual_final_id)
                        abs_cwd = str(manual_prepped.parent.resolve())
                        st.write("### 🔍 Logs")
                        run_command_ui(f"{npx} --yes swagger-cli validate {manual_prepped.name}", cwd=abs_cwd)
                        run_command_ui(f"{npx} --yes rdme openapi validate {manual_prepped.name}", cwd=abs_cwd)

                with col_mu:
                    if st.button("☁️ Validate & Upload Custom Spec", type="primary"):
                        if not manual_final_id.strip():
                            st.error("❌ Target ReadMe Slug cannot be empty.")
                        else:
                            manual_prepped = prep_openapi_file(manual_path, target_version, manual_final_id)
                            abs_cwd = str(manual_prepped.parent.resolve())
                            st.write("### 🔍 Logs")
                            
                            v1 = run_command_ui(f"{npx} --yes swagger-cli validate {manual_prepped.name}", cwd=abs_cwd)
                            v2 = run_command_ui(f"{npx} --yes rdme openapi validate {manual_prepped.name}", cwd=abs_cwd)
                            
                            if v2 == 0:
                                if v1 != 0:
                                    st.warning("⚠️ Swagger-CLI flagged issues, but ReadMe validation passed. Proceeding...")
                                else:
                                    st.success(f"✅ Validations passed. Uploading custom file `{manual_prepped.name}`...")
                                
                                # Targeting .json to overwrite cleanly in ReadMe Refactored
                                upload_cmd = f"{npx} --yes rdme openapi upload {manual_prepped.name} --key {readme_key} --slug {manual_final_id}.json --branch {target_version}"
                                
                                if run_command_ui(upload_cmd, cwd=abs_cwd, mask_secrets=[readme_key]) == 0:
                                    st.success("🎉 Successfully uploaded Custom File to ReadMe!")
                                    
                                    # Auto-update GitHub database if it's a new API
                                    if is_manual_new:
                                        with st.spinner("Pushing new slug to App repo..."):
                                            current_mapping[manual_path.stem] = manual_final_id
                                            if save_slug_mapping(app_repo_name, svc_git_token, current_mapping, current_sha):
                                                st.success(f"📝 Added `'{manual_path.stem}': '{manual_final_id}'` to `slug_mapping.json`.")
                                else:
                                    st.error("❌ Upload failed. See logs above.")

    # ==========================================
    # TAB 3: PULL TO MINTLIFY
    # ==========================================
    with tab_mintlify:
        st.subheader("🌿 Pull Specs from ReadMe → Mintlify Branch")
        st.info(
            "Downloads all specs for the selected ReadMe versions from the ReadMe v2 API "
            "and commits them to `elena/testNavigationChanges` in `alation-dcx`, "
            "then patches `docs.json` with the correct navigation structure."
        )

        MINTLIFY_REPO = st.secrets.get("MINTLIFY_REPO_NAME", "")
        MINTLIFY_BRANCH = "elena/testNavigationChanges"
        DOCS_JSON_PATH = "mintlify-poc-docs/docs.json"
        API_REF_BASE = "mintlify-poc-docs/api-reference"

        # Full version map: ReadMe slug → canonical display name
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

        gh_headers_personal = {
            "Authorization": f"token {git_token}",
            "Accept": "application/vnd.github.v3+json"
        }

        selected_versions = st.multiselect(
            "Select ReadMe versions to pull",
            options=list(VERSION_MAP.keys()),
            format_func=lambda v: f"{VERSION_MAP[v]}  ({v})",
            default=["v2026.5.0-0"],
            help="These map to ReadMe branch slugs. The display name in Mintlify uses the canonical version (e.g. 2026.5.0.0)."
        )

        st.caption(f"🎯 Target repo: `{MINTLIFY_REPO}` on branch `{MINTLIFY_BRANCH}`")

        if st.button("⬇️ Pull Specs & Update docs.json", type="primary"):
            if not selected_versions:
                st.error("❌ Please select at least one version.")
            elif not MINTLIFY_REPO:
                st.error("❌ MINTLIFY_REPO_NAME secret is missing.")
            elif not git_token:
                st.error("❌ GIT_TOKEN secret is missing.")
            elif not readme_key:
                st.error("❌ README_API_KEY secret is missing.")
            else:
                all_versions = []
                any_failures = False

                for readme_version in selected_versions:
                    display_version = VERSION_MAP[readme_version]
                    st.markdown(f"---\n#### 📦 `{display_version}` (ReadMe: `{readme_version}`)")
                    groups = []
                    skipped = []
                    failed = []

                    # Deduplicate slugs (e.g. cde appears twice in slug_mapping)
                    seen_slugs = set()
                    unique_mapping = {}
                    for k, v in current_mapping.items():
                        if v not in seen_slugs:
                            seen_slugs.add(v)
                            unique_mapping[k] = v

                    for eng_key, readme_slug in unique_mapping.items():
                        # ReadMe v2 API: GET /branches/{branch}/apis/{filename}
                        # Slugs in ReadMe are stored as {name}.json
                        fetch_url = (
                            f"https://api.readme.com/v2/branches/{readme_version}"
                            f"/apis/{readme_slug}.json"
                        )
                        resp = requests.get(
                            fetch_url,
                            headers={"Authorization": f"Bearer {readme_key}"}
                        )

                        if resp.status_code == 200:
                            # v2 GET /branches/{branch}/apis/{filename} returns metadata.
                            # Extract the raw spec content from the response data.
                            resp_data = resp.json().get("data", {})
                            source_url = (resp_data.get("source") or {}).get("url")

                            if source_url:
                                spec_resp = requests.get(source_url)
                                spec_content = spec_resp.content
                            else:
                                # No source URL (CLI-uploaded) — re-fetch requesting raw spec
                                spec_resp = requests.get(
                                    f"https://api.readme.com/v2/branches/{readme_version}"
                                    f"/apis/{readme_slug}.json",
                                    headers={
                                        "Authorization": f"Bearer {readme_key}",
                                        "Accept": "application/octet-stream"
                                    }
                                )
                                spec_content = spec_resp.content
                            file_path_in_repo = (
                                f"{API_REF_BASE}/{display_version}/{readme_slug}.json"
                            )
                            commit_url = (
                                f"https://api.github.com/repos/{MINTLIFY_REPO}"
                                f"/contents/{file_path_in_repo}"
                            )

                            # Check if file already exists on branch (need SHA to update)
                            existing = requests.get(
                                commit_url,
                                headers=gh_headers_personal,
                                params={"ref": MINTLIFY_BRANCH}
                            )
                            existing_sha = (
                                existing.json().get("sha")
                                if existing.status_code == 200
                                else None
                            )

                            encoded = base64.b64encode(spec_content).decode("utf-8")
                            commit_payload = {
                                "message": (
                                    f"🤖 Pull {readme_slug} for {display_version} from ReadMe"
                                ),
                                "content": encoded,
                                "branch": MINTLIFY_BRANCH,
                            }
                            if existing_sha:
                                commit_payload["sha"] = existing_sha

                            put_resp = requests.put(
                                commit_url,
                                headers=gh_headers_personal,
                                json=commit_payload
                            )

                            if put_resp.status_code in [200, 201]:
                                group_name = readme_slug.replace("-", " ").title()
                                groups.append({
                                    "group": group_name,
                                    "openapi": f"api-reference/{display_version}/{readme_slug}.json"
                                })
                            else:
                                failed.append(readme_slug)
                                any_failures = True
                                st.error(
                                    f"❌ Failed to commit `{readme_slug}`: "
                                    f"{put_resp.json().get('message', put_resp.text)}"
                                )

                        elif resp.status_code == 404:
                            skipped.append(readme_slug)
                        else:
                            failed.append(readme_slug)
                            any_failures = True
                            st.error(
                                f"❌ Error fetching `{readme_slug}` "
                                f"({resp.status_code}): {resp.text[:200]}"
                            )

                    # Per-version summary
                    st.success(f"✅ Committed {len(groups)} spec(s) for `{display_version}`")
                    if skipped:
                        st.info(
                            f"ℹ️ Skipped {len(skipped)} spec(s) not present in `{readme_version}`: "
                            + ", ".join(f"`{s}`" for s in skipped)
                        )
                    if failed:
                        st.warning(
                            f"⚠️ {len(failed)} spec(s) failed for `{readme_version}`: "
                            + ", ".join(f"`{s}`" for s in failed)
                        )

                    all_versions.append({
                        "version": display_version,
                        "groups": groups
                    })

                # ---- Patch docs.json ----
                st.markdown("---\n#### 📝 Patching `docs.json`")

                docs_url = (
                    f"https://api.github.com/repos/{MINTLIFY_REPO}"
                    f"/contents/{DOCS_JSON_PATH}"
                )
                existing_docs_resp = requests.get(
                    docs_url,
                    headers=gh_headers_personal,
                    params={"ref": MINTLIFY_BRANCH}
                )

                if existing_docs_resp.status_code != 200:
                    st.error(
                        f"❌ Could not fetch `docs.json` from `{MINTLIFY_BRANCH}`: "
                        f"{existing_docs_resp.json().get('message', '')}"
                    )
                else:
                    docs_data = json.loads(
                        base64.b64decode(existing_docs_resp.json()["content"])
                    )
                    docs_sha = existing_docs_resp.json()["sha"]

                    # Find the API Reference tab and replace with versions
                    patched = False
                    for tab in docs_data.get("navigation", {}).get("tabs", []):
                        if tab.get("tab") == "API Reference":
                            tab.pop("dropdowns", None)
                            tab.pop("groups", None)
                            tab.pop("pages", None)
                            tab["versions"] = all_versions
                            patched = True
                            break

                    if not patched:
                        st.error(
                            "❌ Could not find an `API Reference` tab in `docs.json`. "
                            "Check that the tab name matches exactly."
                        )
                    else:
                        # Show the API Reference tab as it will be committed
                        for tab in docs_data.get("navigation", {}).get("tabs", []):
                            if tab.get("tab") == "API Reference":
                                st.write("**Preview of API Reference tab in docs.json:**")
                                st.json(tab)
                                break
                        updated_content = base64.b64encode(
                            json.dumps(docs_data, indent=2).encode("utf-8")
                        ).decode("utf-8")

                        put_docs_resp = requests.put(
                            docs_url,
                            headers=gh_headers_personal,
                            json={
                                "message": (
                                    "🤖 Update docs.json API Reference for: "
                                    + ", ".join(VERSION_MAP[v] for v in selected_versions)
                                ),
                                "content": updated_content,
                                "sha": docs_sha,
                                "branch": MINTLIFY_BRANCH,
                            }
                        )

                        if put_docs_resp.status_code in [200, 201]:
                            if any_failures:
                                st.warning(
                                    "⚠️ `docs.json` updated, but some specs failed to commit. "
                                    "Review errors above before merging."
                                )
                            else:
                                st.success(
                                    "🎉 All done! `docs.json` updated on "
                                    f"`{MINTLIFY_BRANCH}`. "
                                    "Mintlify will auto-deploy the branch preview shortly."
                                )
                        else:
                            st.error(
                                f"❌ Failed to update `docs.json`: "
                                f"{put_docs_resp.json().get('message', put_docs_resp.text)}"
                            )

if __name__ == "__main__":
    main()
