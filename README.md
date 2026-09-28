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

## 🎨 Branding

The app UI uses Alation's actual brand assets, not a generic Streamlit theme:
- **`assets/fonts/`** — Inter (Body & UI) and Redaction (Headings), the real font files (see each font's `-OFL.txt` for license), embedded directly in `app.py` as base64 `@font-face` rules — no external font host needed.
- **`assets/Alation-Logo-Primary-Black.svg`** / **`Alation-Favicon-Black.png`** — the real logo and favicon, rendered above the title and set as the browser tab icon.
- **`.streamlit/config.toml`** — the brand color palette (Whiteboard/Chalk/Graphite/Alation Orange).

Colors are confirmed from the official logo SVGs' fill values, not the Brandguide PDF — that PDF currently lists Graphite as `#9FA1B2` (a duplicate of Napkin) by mistake; the real value is `#14141A`. Worth flagging to whoever owns that PDF. Geist Mono (the Brandguide's "Tech Data" font, used for CLI log output) isn't in this asset package yet — `app.py` names it in the font stack so it picks up automatically if design provides it later, falling back to a standard monospace font until then.

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

   # Git PAT for pulling the Engineering repository (Specs), and for the
   # Service Account that updates slug_mapping.json in this App's repo.
   # Skip these two if you configure the GitHub App secrets below instead --
   # see "GitHub auth" underneath.
   GIT_USER = "your_github_username"
   GIT_TOKEN = "your_personal_access_token"
   SVC_GIT_TOKEN = "your_service_account_github_token"

   ENG_REPO_URL = "[https://github.com/your-org/engineering-repo.git](https://github.com/your-org/engineering-repo.git)"

   # Engineering repo paths to search for YAML files
   PATH_SPECS_MAIN = "django/static/swagger/specs"
   PATH_SPECS_LOGICAL = "django/static/swagger/specs/logical_metadata"

   APP_REPO_NAME = "your-org/this-app-repo-name"

   # --- GitHub auth (optional, replaces GIT_TOKEN/SVC_GIT_TOKEN above) ---
   # A GitHub App's installation token instead of a personal access token --
   # short-lived (~1hr), scoped to just the repos the App is installed on,
   # not tied to a person. One App installation is assumed to cover both
   # ENG_REPO_URL and APP_REPO_NAME. If set, these take priority over
   # GIT_TOKEN/SVC_GIT_TOKEN above; if left out entirely, the PAT fields
   # above are used exactly as before -- this is a drop-in replacement, not
   # a breaking change.
   GITHUB_APP_ID = "your_github_app_id"
   GITHUB_APP_INSTALLATION_ID = "your_installation_id"
   GITHUB_APP_PRIVATE_KEY = """
   -----BEGIN RSA PRIVATE KEY-----
   ...
   -----END RSA PRIVATE KEY-----
   """
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
