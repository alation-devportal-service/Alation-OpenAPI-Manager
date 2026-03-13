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

# --- NODE.JS SETUP (For Streamlit Cloud) ---
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

# --- HELPER FUNCTIONS ---
# --- HELPER FUNCTIONS ---
def run_cmd(cmd_list, cwd=None, hide_cmd=False):
    if not hide_cmd:
        st.write(f"*> Running: {' '.join(cmd_list)}*")
        
    process = subprocess.Popen(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cwd)
    output = []
    
    for line in process.stdout:
        # Extra safety: strip the token from any output logs just in case
        safe_line = line.replace(st.secrets.get("GIT_TOKEN", ""), "***")
        if not hide_cmd:
            st.text(safe_line.strip())
        output.append(safe_line.strip())
        
    process.wait()
    return process.returncode, "\n".join(output)

def prep_openapi_file(filepath, version):
    """
    Modifies the OpenAPI file in-place before validation/upload.
    """
    with open(filepath, "r") as f: 
        data = yaml.safe_load(f)
    
    # 1. Update Version
    if "info" not in data: 
        data["info"] = {}
    data["info"]["version"] = version
    
    # 2. Disable "Try It" (Explorer) and enable Proxy
    if "x-readme" not in data:
        data["x-readme"] = {}
    data["x-readme"]["explorer-enabled"] = False
    data["x-readme"]["proxy-enabled"] = True

    # 3. Update Server Variables to match Alation docs standard
    if "servers" in data and isinstance(data["servers"], list):
        for server in data["servers"]:
            if "variables" in server:
                # Force protocol to default to HTTPS
                if "protocol" in server["variables"]:
                    server["variables"]["protocol"]["default"] = "https"
                
                # Force base-url to default to "alation_domain"
                if "base-url" in server["variables"]:
                    server["variables"]["base-url"]["default"] = "alation_domain"

    # Overwrite the original file exactly as it was named
    with open(filepath, "w") as f: 
        yaml.dump(data, f, sort_keys=False)
        
    return filepath

def execute_validations(npx, filename, abs_cwd, do_swag, do_redoc, do_readme):
    st.write("### 🔍 Validation Logs")
    failed = False
    
    if do_swag:
        if run_cmd([npx, "--yes", "swagger-cli", "validate", filename], cwd=abs_cwd)[0] != 0: failed = True
    if do_redoc:
        if run_cmd([npx, "--yes", "@redocly/cli@1.25.0", "lint", filename], cwd=abs_cwd)[0] != 0: failed = True
    if do_readme:
        if run_cmd([npx, "--yes", "rdme@latest", "openapi:validate", filename], cwd=abs_cwd)[0] != 0: failed = True
    return not failed

# --- MAIN APP ---
def main():
    ensure_node_installed()
    st.title("📘 Alation OpenAPI Manager")
    st.markdown("Easily review Engineering PRs or Release specs to the Developer Portal.")

    # --- SECRETS RETRIEVAL ---
    readme_key = st.secrets.get("README_API_KEY", "")
    git_token = st.secrets.get("GIT_TOKEN", "")
    git_user = st.secrets.get("GIT_USER", "")
    eng_repo_url = st.secrets.get("ENG_REPO_URL", "https://github.com/Alation/alation.git")
    
    path_main = st.secrets.get("PATH_SPECS_MAIN", "django/static/swagger/specs")
    path_logical = st.secrets.get("PATH_SPECS_LOGICAL", "django/static/swagger/specs/logical_metadata")

    if not readme_key or not git_token:
        st.error("🚨 **Configuration Error:** Backend secrets are missing. Please configure Streamlit Secrets.")
        st.stop()

    workspace_dir = Path("./temp_eng_workspace")

    # --- UI SIDEBAR ---
    with st.sidebar:
        st.header("⚙️ Task Configuration")
        eng_branch = st.text_input("Branch to pull from", value="master")
        target_version = st.text_input("API Version to tag (e.g., 3.0.0)", value="1.0.0")
        st.divider()
        st.caption(f"🔒 App is securely connected to: \n`{eng_repo_url}`")

    # --- STEP 1: CLONE ---
    if st.button(f"📥 1. Pull Specs from `{eng_branch}`"):
        if workspace_dir.exists(): shutil.rmtree(workspace_dir)
        workspace_dir.mkdir()
        
        parsed = urllib.parse.urlparse(eng_repo_url)
        auth_url = urllib.parse.urlunparse((parsed.scheme, f"{git_user}:{git_token}@{parsed.netloc}", parsed.path, "", "", ""))
        
        with st.spinner(f"Cloning branch '{eng_branch}'..."):
            code, _ = run_cmd(["git", "clone", "--depth", "1", "--branch", eng_branch, auth_url, str(workspace_dir)], hide_cmd=True)
            if code == 0: 
                st.success(f"✅ Successfully pulled engineering files from branch: `{eng_branch}`")
            else: 
                st.error("❌ Failed to pull repository. Ensure the branch exists and token is valid.")

    # --- STEP 2: SELECT & PROCESS ---
    if workspace_dir.exists():
        st.divider()
        st.subheader("🛠️ 2. Select API Spec")
        
        yaml_files = []
        for p in [path_main, path_logical]:
            target_path = workspace_dir / p
            if target_path.exists():
                yaml_files.extend(list(target_path.glob("*.yaml")))
        
        file_options = [f.name for f in yaml_files]
        
        if not file_options:
            st.warning("No YAML files found in the configured paths.")
            return

        selected_file_name = st.selectbox("Select Spec to Manage", sorted(file_options))
        selected_file_path = next(f for f in yaml_files if f.name == selected_file_name)
        
        mapped_id = SLUG_MAPPING.get(selected_file_path.stem, "")
        
        col1, col2 = st.columns(2)
        with col1: st.info(f"**Selected File:** `{selected_file_name}` \n\n*Path: `{selected_file_path.relative_to(workspace_dir)}`*")
        with col2:
            if mapped_id: st.success(f"**Target ReadMe ID:** `{mapped_id}`")
            else: st.error("⚠️ **No ID Mapped.** This file is orphaned in the dictionary.")
            
        final_id = st.text_input("Confirm/Edit ReadMe ID (JSON filename):", value=mapped_id)

        # --- STEP 3: ACTION TABS ---
        st.divider()
        st.subheader("🚀 3. Choose Action")
        
        tab1, tab2 = st.tabs(["🔍 Validate Only (PR Review)", "☁️ Upload to ReadMe (Release)"])
        
        npx = shutil.which("npx")
        
        # TAB 1: PR REVIEW
        with tab1:
            st.markdown("Run linters to check for OpenAPI errors. **Nothing will be uploaded.**")
            c1, c2, c3 = st.columns(3)
            do_swag_val = c1.checkbox("Swagger Validation", value=True, key="v_swag")
            do_redoc_val = c2.checkbox("Redocly Validation", value=False, key="v_redoc") 
            do_rm_val = c3.checkbox("ReadMe Validation", value=True, key="v_rm")
            
            if st.button("Run Validations Only", disabled=not final_id):
                prepped_file = prep_openapi_file(selected_file_path, target_version)
                passed = execute_validations(npx, prepped_file.name, str(prepped_file.parent.resolve()), do_swag_val, do_redoc_val, do_rm_val)
                if passed: st.success("✅ Spec passed all selected validations! Safe to approve PR.")
                else: st.error("❌ Spec failed validation. Do not merge PR.")

        # TAB 2: RELEASE UPLOAD
        with tab2:
            st.markdown("Run linters and push directly to your live Developer Portal.")
            c1, c2, c3 = st.columns(3)
            do_swag_up = c1.checkbox("Swagger Validation", value=True, key="u_swag")
            do_redoc_up = c2.checkbox("Redocly Validation", value=False, key="u_redoc") 
            do_rm_up = c3.checkbox("ReadMe Validation", value=True, key="u_rm")

            if st.button("Validate & Upload", type="primary", disabled=not final_id):
                prepped_file = prep_openapi_file(selected_file_path, target_version)
                abs_cwd = str(prepped_file.parent.resolve())
                filename = prepped_file.name
                
                passed = execute_validations(npx, filename, abs_cwd, do_swag_up, do_redoc_up, do_rm_up)
                
                if not passed:
                    st.error("❌ Validation failed. Upload aborted.")
                else:
                    st.success("✅ Validations passed! Starting Upload...")
                    
                    # EXACT MATCH UPLOAD USING shell=True (Bypasses array parsing issues)
                    raw_cmd = f"{npx} --yes rdme@latest openapi {filename} --key {readme_key} --id {final_id} --version {target_version}"
                    
                    st.write(f"*> Running: {raw_cmd.replace(readme_key, '***')}*")
                    
                    process = subprocess.Popen(raw_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=abs_cwd)
                    up_logs_list = []
                    
                    for line in process.stdout:
                        safe_line = line.replace(readme_key, "***")
                        st.text(safe_line.strip())
                        up_logs_list.append(safe_line.strip())
                        
                    process.wait()
                    up_code = process.returncode
                    up_logs = "\n".join(up_logs_list)
                    
                    if up_code == 0:
                        st.success(f"🎉 Successfully uploaded `{filename}` to ReadMe container `{final_id}`!")
                    else:
                        st.error("❌ Upload failed.")
                        if "403" in up_logs and "Git-backed" in up_logs:
                            st.error("🚨 CRITICAL: ReadMe blocked the direct upload because your project is in Refactored (Git-backed) mode. Direct API uploads are disabled for this project type.")

if __name__ == "__main__":
    main()
