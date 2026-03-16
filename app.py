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

    # STEP 2: SELECT
    if workspace_dir.exists():
        st.divider()
        st.subheader("🛠️ 2. Select API Spec")
        yaml_files = []
        for p in [path_main, path_logical]:
            tp = workspace_dir / p
            if tp.exists(): 
                # NEW: Filter out the temporary prepped files from the UI!
                valid_files = [f for f in tp.glob("*.yaml") if not f.name.endswith("_prepped.yaml")]
                yaml_files.extend(valid_files)
        
        file_options = sorted([f.name for f in yaml_files])
        if not file_options: return

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
        tab1, tab2 = st.tabs(["🔍 Validate Only", "☁️ Upload to ReadMe"])
        npx = shutil.which("npx")
        
        with tab1:
            if st.button("Run Validations"):
                prepped = prep_openapi_file(selected_file_path, target_version, final_id)
                abs_cwd = str(prepped.parent.resolve())
                st.write("### 🔍 Logs")
                run_command_ui(f"{npx} --yes swagger-cli validate {prepped.name}", cwd=abs_cwd)
                run_command_ui(f"{npx} --yes rdme openapi validate {prepped.name}", cwd=abs_cwd)

        with tab2:
            if st.button("Validate & Upload", type="primary"):
                if not final_id.strip():
                    st.error("❌ Target ReadMe Slug cannot be empty.")
                else:
                    prepped = prep_openapi_file(selected_file_path, target_version, final_id)
                    abs_cwd = str(prepped.parent.resolve())
                    st.write("### 🔍 Logs")
                    
                    v1 = run_command_ui(f"{npx} --yes swagger-cli validate {prepped.name}", cwd=abs_cwd)
                    v2 = run_command_ui(f"{npx} --yes rdme openapi validate {prepped.name}", cwd=abs_cwd)
                    
                    if v1 == 0 and v2 == 0:
                        st.success(f"✅ Validations passed. Uploading as `{prepped.name}`...")
                        upload_cmd = f"{npx} --yes rdme openapi upload {prepped.name} --key {readme_key} --slug {final_id}.json --branch {target_version}"
                        
                        # THE CRITICAL CHECK: Did the upload actually succeed?
                        if run_command_ui(upload_cmd, cwd=abs_cwd, mask_secrets=[readme_key]) == 0:
                            st.success("🎉 Successfully uploaded to ReadMe!")
                            
                            # ONLY update GitHub if it's a new file AND upload succeeded
                            if is_new_file:
                                with st.spinner("Pushing new slug to App repo..."):
                                    current_mapping[selected_file_path.stem] = final_id
                                    saved = save_slug_mapping(app_repo_name, svc_git_token, current_mapping, current_sha)
                                    if saved:
                                        st.success(f"📝 Added `'{selected_file_path.stem}': '{final_id}'` to `slug_mapping.json`.")
                                    else:
                                        st.warning("⚠️ Upload succeeded, but failed to save the mapping to GitHub. Please check service account permissions.")
                        else:
                            st.error("❌ Upload failed. See logs above. (Database not updated).")

if __name__ == "__main__":
    main()
