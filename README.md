# 📘 Alation OpenAPI Manager

A Streamlit-based web application designed to streamline the management, validation, and publishing of OpenAPI specifications. 

This tool integrates directly with GitHub to pull your engineering repositories, resolves OpenAPI `$ref` dependencies, validates the YAML specifications using Node.js tools (`swagger-cli` and `rdme`), and uploads them directly to ReadMe. It also maintains and auto-updates an API slug mapping database (`slug_mapping.json`) in your application repository.

## ✨ Features

* **Git Integration:** Pull OpenAPI specs directly from your engineering repository using a specified branch.
* **Automated Environment Setup:** Automatically downloads and configures a local Node.js runtime if one isn't detected, ensuring `swagger-cli` and `rdme` work out of the box.
* **Validation Pipeline:** Runs strict schema validations using `swagger-cli` and ReadMe's official CLI before allowing uploads.
* **Auto-Slug Mapping:** Generates clean URL slugs for new APIs and commits them back to a `slug_mapping.json` file in your GitHub repository using a service account.
* **Manual File Override:** Upload a locally modified YAML file while preserving the Git repository context, ensuring multi-file `$ref` dependencies still resolve perfectly.
* **Pre-processing:** Automatically preps your OpenAPI files before upload (e.g., disabling ReadMe's default explorer, enforcing HTTPS, and injecting the correct base URLs).

## 📋 Prerequisites

* **Python:** 3.8+
* **Git:** Installed and available in your system's PATH.
* **Node.js:** (Optional) The app will automatically download a portable version of Node v20.11.0 if it isn't installed globally.

## 🚀 Installation & Setup

1. **Clone the repository:**
   
   ```bash
   git clone <your-app-repo-url>
   cd <your-app-directory>
   ```
2. **Install Python dependencies:**
   It is recommended to use a virtual environment.
   
   ```bash
   pip install streamlit pyyaml requests
   ```
3. **Configure Secrets:**
   Streamlit requires a secrets file to manage API keys and Git tokens. Create a directory named `.streamlit` in the root of your project, and create a file inside it called `secrets.toml`.
   `.streamlit/secrets.toml`

   ```ini,toml
   # ReadMe API Configuration
   README_API_KEY = "your_readme_api_key_here"

   # Git PAT for pulling the Engineering repository (Specs)
   GIT_USER = "your_github_username"
   GIT_TOKEN = "your_personal_access_token"
   ENG_REPO_URL = "[https://github.com/your-org/engineering-repo.git](https://github.com/your-org/engineering-repo.git)"

   # Engineering repo paths to search for YAML files
   PATH_SPECS_MAIN = "django/static/swagger/specs"
   PATH_SPECS_LOGICAL = "django/static/swagger/specs/logical_metadata"
   
   # Service Account for updating slug_mapping.json in this App's repo
   SVC_GIT_TOKEN = "your_service_account_github_token"
   APP_REPO_NAME = "your-org/this-app-repo-name"
   ```
## 💻 Usage
  Run the Streamlit app locally:

  ```bash
  streamlit run app.py
  ```
## Workflow

Configure Task: Set your target ReadMe version and Engineering branch in the sidebar.

1. Pull Specs: Click **1. Pull Specs** to clone the engineering repo into a temporary workspace.

2. Choose your Pipeline:

- **Git Repo Pipeline:** Select a spec from the dropdown, verify its mapped ReadMe slug, and click **Validate & Upload**.

- **Manual File Upload:** If you have local edits, upload your YAML file here. The app will inject it into the cloned repo context to maintain dependency links before validating and uploading.

## 📄 License
This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along with this program. If not, see [licenses](https://www.gnu.org/licenses/).
