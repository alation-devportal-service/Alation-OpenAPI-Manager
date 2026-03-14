import streamlit as st
import yaml
import subprocess
import shutil
import requests
import os
import urllib.parse
import tarfile
from pathlib import Path

# --- PAGE CONFIG ---
st.set_page_config(page_title="Alation OpenAPI Manager", page_icon="📘", layout="wide")

# --- VERIFIED ID DICTIONARY ---
SLUG_MAPPING = {
    "agent": "alation-agent-api",
    "api_authentication": "token-authentication-and-management-apis",
    "articles": "articles-api",
    "connectors": "connector-apis",
    "context": "aggregated-context-api",
    "conversations": "conversations-api-version-2", 
    "data_dictionary": "data-dictionary-api",
    "data_products": "data-products-api",
    "data_quality": "data-health-api", 
    "datasources": "datasources-api",
    "document": "documents-api",
    "domain": "domain-api",
    "folder": "folder-api",
    "gbmv2": "bi-source-api", 
    "group": "group-public-api",
    "homepage": "homepage-preferences-api",
    "integration_apis": "relational-integration-api",
    "lineage": "lineage-v2-api",
    "lineage_v3": "lineage-api-v3",
    "logs": "logging-api",
    "members_permission": "members-permission-api",
    "my_domains": "my-domains-api",
    "native_data_quality": "native-data-quality-api",
    "nosql": "nosql-data-sources-api",
    "oauth": "oauth-20-apis-for-managing-clients-and-user-authorization",
    "oauthv2": "oauth-20-apis-for-service-authorization",
    "ocf_datasources": "data-sources-api-ocf",
    "otypes": "otypes-api",
    "policy": "policy-api",
    "privacy_settings": "privacy-settings-api",
    "scimv2": "scim-20-api",
    "search": "search-api",
    "terms": "terms-api",
    "user": "user-public-api",
    "userv2": "user-public-api-1",
    "visual_config": "template-visual-config-api",
    "workflows": "workflows-api",
    "field": "custom-fields-api",
    "field_value": "custom-field-values-api"
}

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

# --- STABILIZED COMMAND RUNNER ---
def run_command_ui(cmd_string, cwd=None, mask_secrets=[]):
    """
    Executes a shell command and streams output to UI.
    Uses shell=True to fix the 'command not found' space issue.
    """
    display_cmd = cmd_string
    for s in mask_secrets:
        if s: display_cmd = display_cmd.replace(s, "***")
    
    st.write(f"*> Running: {display_cmd}*")
    
    process = subprocess.Popen(
        cmd_string,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd
    )
    
    for line in process.stdout:
        clean_line = line.strip()
        for s in mask_secrets:
            if s: clean_line = clean_line.replace(s, "***")
        st.text(clean_line)
        
    process.wait()
    return process.returncode

def prep_openapi_file(filepath, version):
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

    with open(filepath, "w") as f: yaml.dump(data, f, sort_keys=False)
    return filepath

# --- MAIN APP ---
def main():
    ensure_node_installed()
    st.title("📘 Alation OpenAPI Manager")
    
    readme_key = st.secrets.get("README_API_KEY", "")
    git_token = st.secrets.get("GIT_TOKEN", "")
    git_user = st.secrets.get("GIT_USER", "")
    eng_repo_url = st.secrets.get("ENG_REPO_URL", "https://github.com/Alation/alation.git")
    path_main = st.secrets.get("PATH_SPECS_MAIN", "django/static/swagger/specs")
    path_logical = st.secrets.get("PATH_SPECS_LOGICAL", "django/static/swagger/specs/logical_metadata")

    workspace_dir = Path("./temp_eng_workspace")

    with st.sidebar:
        st.header("⚙️ Task Configuration")
        eng_branch = st.text_input("Engineering Branch", value="master")
        target_version = st.text_input("ReadMe Version/Branch", value="v2026.3.1-0_api-spec-test")
        st.divider()
        st.caption(f"🔒 Connected to: `{eng_repo_url}`")

    # STEP 1: PULL
    if st.button(f"📥 1. Pull Specs from `{eng_branch}`"):
        if workspace_dir.exists(): shutil.rmtree(workspace_dir)
        workspace_dir.mkdir()
        parsed = urllib.parse.urlparse(eng_repo_url)
        auth_url = urllib.parse.urlunparse((parsed.scheme, f"{git_user}:{git_token}@{parsed.netloc}", parsed.path, "", "", ""))
        with st.spinner("Cloning..."):
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
            if tp.exists(): yaml_files.extend(list(tp.glob("*.yaml")))
        
        file_options = sorted([f.name for f in yaml_files])
        if not file_options: return

        selected_file_name = st.selectbox("Select Spec", file_options)
        selected_file_path = next(f for f in yaml_files if f.name == selected_file_name)
        mapped_id = SLUG_MAPPING.get(selected_file_path.stem, "")
        
        col1, col2 = st.columns(2)
        col1.info(f"**File:** `{selected_file_name}`")
        if mapped_id: col2.success(f"**ReadMe ID:** `{mapped_id}`")
        else: col2.error("⚠️ No ID Mapped.")
            
        final_id = st.text_input("Target ReadMe ID:", value=mapped_id)

        # STEP 3: ACTIONS
        st.divider()
        st.subheader("🚀 3. Choose Action")
        tab1, tab2 = st.tabs(["🔍 Validate Only", "☁️ Upload to ReadMe"])
        npx = shutil.which("npx")
        
        with tab1:
            if st.button("Run Validations"):
                prepped = prep_openapi_file(selected_file_path, target_version)
                abs_cwd = str(prepped.parent.resolve())
                st.write("### 🔍 Logs")
                run_command_ui(f"{npx} --yes swagger-cli validate {prepped.name}", cwd=abs_cwd)
                run_command_ui(f"{npx} --yes rdme openapi:validate {prepped.name}", cwd=abs_cwd)

        with tab2:
            if st.button("Validate & Upload", type="primary"):
                prepped = prep_openapi_file(selected_file_path, target_version)
                abs_cwd = str(prepped.parent.resolve())
                st.write("### 🔍 Logs")
                
                # 1. Validate
                v1 = run_command_ui(f"{npx} --yes swagger-cli validate {prepped.name}", cwd=abs_cwd)
                v2 = run_command_ui(f"{npx} --yes rdme openapi:validate {prepped.name}", cwd=abs_cwd)
                
                if v1 == 0 and v2 == 0:
                    st.success("✅ Validations passed. Uploading...")
                    # 2. Upload using shell string to bypass list-parsing errors
                    upload_cmd = f"{npx} --yes rdme openapi upload {prepped.name} --key {readme_key} --slug {final_id} --branch {target_version}"
                    
                    if run_command_ui(upload_cmd, cwd=abs_cwd, mask_secrets=[readme_key]) == 0:
                        st.success("🎉 Successfully uploaded to ReadMe!")
                    else:
                        st.error("❌ Upload failed. See logs above.")

if __name__ == "__main__":
    main()
