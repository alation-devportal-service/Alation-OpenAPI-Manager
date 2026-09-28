import streamlit as st
import yaml
import json
import subprocess
import shutil
import requests
import os
import time
import urllib.parse
import tarfile
import re
import base64
import jwt
from pathlib import Path

ASSETS_DIR = Path(__file__).parent / "assets"

# ---------------------------------------------------------------------------
# PAGE CONFIG
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Alation OpenAPI Manager",
    page_icon=str(ASSETS_DIR / "Alation-Favicon-Black.png"),
    layout="wide",
)

# ---------------------------------------------------------------------------
# ALATION BRAND THEME -- real assets, not reconstructed: fonts and logo
# copied from assets/ (originally from the design-provided rebrand
# package; see assets/fonts/*-OFL.txt for license), colors confirmed
# directly from the official logo SVG fill values.
#
# IMPORTANT: Graphite is #14141A, not #9FA1B2 -- the Brandguide PDF
# currently lists Graphite as a duplicate of Napkin's value by mistake;
# #14141A is the real value, taken from the SVG. Worth flagging to
# whoever owns that PDF.
#
# Colors also live in .streamlit/config.toml (the stable, version-proof
# way to theme Streamlit's own native widgets); this covers what
# config.toml can't: the actual Inter/Redaction font files (embedded as
# base64 data URIs, so nothing needs external hosting) and the
# Brandguide's heading letter-spacing/line-height rule. Both must stay
# consistent if either changes.
# ---------------------------------------------------------------------------

def _asset_b64(rel_path):
    return base64.b64encode((ASSETS_DIR / rel_path).read_bytes()).decode("ascii")

def apply_alation_theme():
    redaction_regular = _asset_b64("fonts/Redaction-Regular.woff2")
    redaction_bold    = _asset_b64("fonts/Redaction-Bold.woff2")
    redaction_italic  = _asset_b64("fonts/Redaction-Italic.woff2")
    inter_variable    = _asset_b64("fonts/Inter-Variable.ttf")

    st.markdown(f"""
    <style>
    @font-face {{
        font-family: 'Redaction';
        src: url(data:font/woff2;base64,{redaction_regular}) format('woff2');
        font-weight: 400; font-style: normal; font-display: swap;
    }}
    @font-face {{
        font-family: 'Redaction';
        src: url(data:font/woff2;base64,{redaction_bold}) format('woff2');
        font-weight: 700; font-style: normal; font-display: swap;
    }}
    @font-face {{
        font-family: 'Redaction';
        src: url(data:font/woff2;base64,{redaction_italic}) format('woff2');
        font-weight: 400; font-style: italic; font-display: swap;
    }}
    @font-face {{
        /* Variable font: one file covers weight 100-900 (Inter has no
           width/stretch axis, so no font-stretch declaration here). */
        font-family: 'Inter';
        src: url(data:font/ttf;base64,{inter_variable}) format('truetype');
        font-weight: 100 900; font-style: normal; font-display: swap;
    }}

    :root {{
        --altn-whiteboard: #F9F9FB;
        --altn-chalk: #E7E7EE;
        --altn-napkin: #9FA1B2;
        --altn-graphite: #14141A;
        --altn-orange: #FF9900;
        --altn-highlight-start: #EA5458;
        --altn-highlight-end: #FF9901;
    }}

    /* Body & UI -- Inter, per the Brandguide's Paragraph/Body Small specs.
       The universal selector is deliberate: it has the LOWEST possible
       specificity, so Streamlit's own theme CSS (which targets headings
       and code via its internal data-testid wrappers, at higher
       specificity than a plain tag selector) still loses to the more
       specific rules below rather than to this one. Confirmed necessary:
       a plain `h1, h2, h3 {{...}}` rule here was silently outweighed by
       Streamlit's own heading CSS and never took effect at all.

       Icon elements are explicitly excluded -- Streamlit renders its own
       icons (e.g. the file-uploader's upload icon) via Google's Material
       Symbols, an ICON FONT: specific words like "upload" aren't
       literal text there, they're the exact string that font's glyph
       table maps to an icon shape. Forcing Inter onto those elements
       breaks that mapping and the literal word renders instead of the
       icon -- confirmed real: exactly this happened to the file
       uploader's icon, showing "upload" as visible overlapping text. */
    *:not([data-testid*="Icon"]):not([class*="material"]):not(svg):not(svg *) {{
        font-family: 'Inter', sans-serif !important;
    }}

    /* Headings -- Redaction, per the Brandguide's Headline spec
       (letter-spacing/line-height matches exactly; size is left to each
       heading level's own context rather than the brand's 96pt display
       size, which is a marketing-page treatment, not a tool-UI one).
       Both the bare tag and Streamlit's actual wrapper testids are
       targeted, since which one wins varies by Streamlit version. */
    h1, h2, h3,
    [data-testid="stMarkdownContainer"] h1, [data-testid="stMarkdownContainer"] h2, [data-testid="stMarkdownContainer"] h3,
    [data-testid="stHeading"] h1, [data-testid="stHeading"] h2, [data-testid="stHeading"] h3,
    [data-testid="stHeadingWithActionElements"] h1, [data-testid="stHeadingWithActionElements"] h2, [data-testid="stHeadingWithActionElements"] h3 {{
        font-family: 'Redaction', serif !important;
        letter-spacing: -0.02em;
        line-height: 92%;
        color: var(--altn-graphite);
    }}

    /* Tech Data -- Geist Mono per the Brandguide, but not part of this
       asset package (design hasn't provided the files yet); named here
       so it's picked up automatically if a system/future copy exists,
       falling back to a standard monospace stack otherwise. */
    code, pre, kbd, samp,
    [data-testid="stMarkdownContainer"] code, [data-testid="stMarkdownContainer"] pre,
    [data-testid="stText"], [data-testid="stCodeBlock"] {{
        font-family: 'Geist Mono', 'SFMono-Regular', Consolas, monospace !important;
    }}

    /* Alation Orange primary actions, Graphite on hover -- several
       selector variants since Streamlit's own internal markup for this
       has changed across versions. */
    button[kind="primary"], .stButton > button[kind="primary"],
    [data-testid="stBaseButton-primary"], [data-testid="baseButton-primary"] {{
        background-color: var(--altn-orange) !important;
        border-color: var(--altn-orange) !important;
        color: white !important;
    }}
    button[kind="primary"]:hover, .stButton > button[kind="primary"]:hover,
    [data-testid="stBaseButton-primary"]:hover, [data-testid="baseButton-primary"]:hover {{
        background-color: var(--altn-graphite) !important;
        border-color: var(--altn-graphite) !important;
    }}
    </style>
    """, unsafe_allow_html=True)

def render_alation_header():
    logo_svg_b64 = _asset_b64("Alation-Logo-Primary-Black.svg")
    st.markdown(
        f'<img src="data:image/svg+xml;base64,{logo_svg_b64}" style="height:32px;margin-bottom:0.5rem;">',
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# GITHUB APP AUTH -- replaces a long-lived PAT with a short-lived (~1hr)
# installation access token, minted on demand from a GitHub App's private
# key. This is the standard PAT replacement for automation/service use
# cases (GitHub Apps aren't tied to a person, are scoped to just the repos
# they're installed on, and every token they mint is short-lived and
# individually auditable).
#
# Deliberately optional: if GITHUB_APP_ID/PRIVATE_KEY/INSTALLATION_ID
# aren't all set, resolve_git_auth() falls back to the existing PAT
# (GIT_USER/GIT_TOKEN or SVC_GIT_TOKEN) unchanged -- so this works today,
# before the App is even registered, and switches over with zero code
# change the moment its secrets are added.
# ---------------------------------------------------------------------------

def get_github_app_token(app_id, private_key_pem, installation_id):
    """Mints (or returns the still-valid cached) installation access token
    for this GitHub App installation. Cached in st.session_state per
    installation_id and refreshed a minute before actual expiry, so a
    single Streamlit session doesn't re-mint one on every git/API call."""
    cache_key = f"gh_app_token_{installation_id}"
    cached = st.session_state.get(cache_key)
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["token"]

    now = int(time.time())
    encoded_jwt = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": app_id}, private_key_pem, algorithm="RS256",
    )
    resp = requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={"Authorization": f"Bearer {encoded_jwt}", "Accept": "application/vnd.github+json"},
    )
    resp.raise_for_status()
    data = resp.json()
    expires_at = time.mktime(time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    st.session_state[cache_key] = {"token": data["token"], "expires_at": expires_at}
    return data["token"]

def resolve_git_auth(app_id, private_key_pem, installation_id, fallback_user, fallback_token):
    """Returns (git_user, git_token) for an HTTPS clone URL or a REST
    Authorization header. Prefers a GitHub App installation token --
    paired with the literal 'x-access-token' username GitHub's own docs
    specify for this -- when the App secrets are configured; otherwise
    falls back to the given PAT (fallback_user, fallback_token) as-is."""
    if app_id and private_key_pem and installation_id:
        try:
            return "x-access-token", get_github_app_token(app_id, private_key_pem, installation_id)
        except Exception as e:
            st.warning(f"⚠️ GitHub App token exchange failed ({e}); falling back to PAT.")
    return fallback_user, fallback_token

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
# VALIDATORS
# ---------------------------------------------------------------------------

# Shared between the sidebar's conditional "ReadMe Version" field and both
# tabs' validator multiselects, so the two can never drift out of sync --
# the sidebar has to know rdme's exact option string and default state
# before either multiselect widget has been created for the first time.
VALIDATOR_OPTIONS  = ["swagger-cli", "rdme", "Redocly (lint)", "Mintlify (mint validate)"]
DEFAULT_VALIDATORS = ["swagger-cli", "rdme"]

MINT_VALIDATE_WORKSPACE = Path("./temp_mint_validate_workspace")

def run_mintlify_validation(spec_filepath):
    """Validates spec_filepath against Mintlify's own OpenAPI validator
    (`mint validate`) -- installed on demand via `npx --yes mint@latest`,
    the same on-demand pattern already used for swagger-cli/rdme (confirmed
    a real, publicly published npm package -- 'The Mintlify CLI' -- so this
    works on a fresh machine with no pre-install, same as Streamlit Cloud).

    Deliberately does NOT clone the real acs-docs-mintlify repo or touch
    any branch there. `mint validate` has no single-file mode -- it always
    validates a whole Mintlify project -- but a full project isn't needed
    to check one spec's OpenAPI schema validity: a throwaway, minimal
    scaffold (a fixed docs.json plus this one spec, wired in via a
    group-level 'openapi' field, which validates every operation in the
    file, not just one) is enough. Confirmed directly: this exact scaffold
    surfaces the same schema error mint validate gives inside the real
    repo, and passes clean for a spec with no such issue.

    Builds the scaffold fresh in its own temp workspace on every call,
    tears it down afterward either way, and returns mint's exit code
    (0 = passed)."""
    if MINT_VALIDATE_WORKSPACE.exists():
        shutil.rmtree(MINT_VALIDATE_WORKSPACE)
    openapi_dir = MINT_VALIDATE_WORKSPACE / "openapi"
    openapi_dir.mkdir(parents=True)
    scaffold_spec_name = spec_filepath.name
    shutil.copy(spec_filepath, openapi_dir / scaffold_spec_name)
    docs_json = {
        "$schema": "https://mintlify.com/docs.json",
        "name": "Standalone Validator",
        "theme": "mint",
        "colors": {"primary": "#000000"},
        "navigation": {
            "tabs": [{
                "tab": "API",
                "groups": [{"group": "Validation", "openapi": f"openapi/{scaffold_spec_name}"}],
            }],
        },
    }
    (MINT_VALIDATE_WORKSPACE / "docs.json").write_text(json.dumps(docs_json, indent=2))
    try:
        return run_command_ui("npx --yes mint@latest validate", cwd=str(MINT_VALIDATE_WORKSPACE.resolve()))
    finally:
        shutil.rmtree(MINT_VALIDATE_WORKSPACE, ignore_errors=True)

# ---------------------------------------------------------------------------
# OPENAPI FILE PREP
# ---------------------------------------------------------------------------

class SpecYAMLError(Exception):
    """Raised when a spec file fails to parse as valid YAML/mapping."""
    pass

def _load_yaml_or_report(filepath):
    """Loads a YAML file, raising SpecYAMLError with a precise line/column message on failure.

    Deliberately raises (rather than calling st.error/st.stop itself) so the one caller,
    prep_openapi_file, decides how to handle it -- currently always surface-and-stop.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            problem = getattr(e, "problem", str(e))
            raise SpecYAMLError(
                f"`{filepath.name}` is not valid YAML{location}. Problem: {problem} "
                "(common causes: an unquoted colon inside a description/URL, a stray tab "
                "character, or an unbalanced quote near that line)."
            ) from e
    if not isinstance(data, dict):
        raise SpecYAMLError(
            f"`{filepath.name}` parsed but its top level is a `{type(data).__name__}`, "
            "not a mapping. Check that the file starts with `openapi:`/`info:` keys and "
            "isn't, e.g., a list or a plain string."
        )
    return data

def fix_broken_file_refs(data, filepath, workspace_dir):
    """Recursively walks a parsed OpenAPI dict/list structure and repairs any
    file-relative `$ref` whose target doesn't exist on disk relative to `filepath`.

    Engineering-owned spec files sometimes ship with the wrong number of `../`
    segments (e.g. copy-pasted from a spec one directory level deeper/shallower).
    Rather than failing validation with a raw ENOENT, search the cloned
    `workspace_dir` for a file with the same basename and, if exactly one match
    is found, rewrite the ref to the correct relative path. Mutates `data` in
    place. Returns a list of human-readable strings describing what was fixed
    or what couldn't be resolved, for display via st.info/st.warning.
    """
    notes = []

    def split_ref(ref_value):
        if "#" in ref_value:
            file_part, _, anchor = ref_value.partition("#")
            return file_part, "#" + anchor
        return ref_value, ""

    def walk(node):
        if isinstance(node, dict):
            ref_val = node.get("$ref")
            if isinstance(ref_val, str) and ref_val and not ref_val.startswith(("http://", "https://", "#")):
                file_part, anchor = split_ref(ref_val)
                target_path = (filepath.parent / file_part).resolve()
                if not target_path.exists():
                    basename   = Path(file_part).name
                    candidates = [c for c in workspace_dir.rglob(basename) if c.is_file()]
                    if len(candidates) == 1:
                        new_rel = os.path.relpath(candidates[0], start=filepath.parent).replace(os.sep, "/")
                        node["$ref"] = new_rel + anchor
                        notes.append(f"🔧 Fixed broken ref `{ref_val}` → `{new_rel}{anchor}`")
                    elif len(candidates) > 1:
                        notes.append(
                            f"⚠️ Ref `{ref_val}` doesn't resolve and matched {len(candidates)} "
                            f"files named `{basename}` in the repo — left as-is, please fix manually."
                        )
                    else:
                        notes.append(
                            f"⚠️ Ref `{ref_val}` doesn't resolve and no file named `{basename}` "
                            "was found anywhere in the cloned repo — left as-is."
                        )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return notes

def prep_openapi_file(filepath, target_slug, workspace_dir=None, version=None):
    """For both tabs: writes a prepped YAML file for CLI validation and/or
    upload to ReadMe. `version` is ReadMe-specific (stamped into info.version
    for the upload's own record-keeping) and optional -- omit it for pure
    validation, where none of the four CLIs care what it is; swagger-cli,
    Redocly, and Mintlify never look at it either way, and even rdme's own
    `openapi validate` needs no ReadMe account or version at all (confirmed
    from its own --help text) -- only `openapi upload` does."""
    try:
        data = _load_yaml_or_report(filepath)
    except SpecYAMLError as e:
        st.error(f"❌ {e}")
        st.stop()
    if workspace_dir is not None:
        ref_notes = fix_broken_file_refs(data, filepath, workspace_dir)
        for note in ref_notes:
            (st.warning if note.startswith("⚠️") else st.info)(note)
    if version is not None:
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

# ---------------------------------------------------------------------------
# $REF DEPENDENCY CHECKER
# ---------------------------------------------------------------------------

def find_missing_ref_targets(start_files, workspace_dir):
    """Walks $ref file targets from the given spec files (and transitively, their own
    refs) and returns a sorted list of referenced paths that don't exist on disk.

    Best-effort: matches '$ref: path/to/file.yaml#/Foo' style entries via regex rather
    than a full YAML parse (fast, and tolerant of any file that fails to parse on its
    own). Ignores in-document '#/...' refs and http(s) refs.
    """
    seen = set()
    missing = set()
    queue = list(start_files)
    while queue:
        f = queue.pop()
        if f in seen or not f.exists():
            continue
        seen.add(f)
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in re.finditer(r"\$ref:\s*['\"]?([^'\"#\s]+)", text):
            target = m.group(1)
            if not target or target.startswith(("http://", "https://")):
                continue
            target_path = (f.parent / target).resolve()
            if not target_path.exists():
                try:
                    rel = target_path.relative_to(workspace_dir.resolve())
                except ValueError:
                    rel = target_path
                missing.add(str(rel))
            elif target_path.suffix.lower() in (".yaml", ".yml", ".json") and target_path not in seen:
                queue.append(target_path)
    return sorted(missing)

# ---------------------------------------------------------------------------
# UPSTREAM YAML WORKAROUNDS
# ---------------------------------------------------------------------------

def patch_known_upstream_yaml_bugs(workspace_dir, path_main):
    """Temporary workaround for a known bug in the engineering repo's
    common/responses.yaml: several `detail` example strings wrap onto a second,
    under-indented line, which fails strict YAML 1.2 parsing (rdme) even though
    it passes swagger-cli's lenient parser. Collapses each wrapped string onto
    one line -- semantically identical, since YAML already folds that line break
    into a single space. Safe to leave in place: it's a no-op once the upstream
    fix merges. Remove once it does, to avoid the workaround outliving its reason.
    """
    target = workspace_dir / path_main / "common" / "responses.yaml"
    if not target.exists():
        return False
    text = target.read_text(encoding="utf-8")
    patched, n = re.subn(
        r'(detail: "[^"\n]*)\n\s+(\(Refer[^"\n]*")',
        r'\1 \2',
        text,
    )
    if n:
        target.write_text(patched, encoding="utf-8")
    return n > 0

# ---------------------------------------------------------------------------
# MAIN APP
# ---------------------------------------------------------------------------

def main():
    ensure_node_installed()
    apply_alation_theme()
    render_alation_header()
    st.title("OpenAPI Manager")

    # --- Secrets ---
    readme_key    = st.secrets.get("README_API_KEY", "")
    eng_repo_url  = st.secrets.get("ENG_REPO_URL", "")
    path_main     = st.secrets.get("PATH_SPECS_MAIN", "django/static/swagger/specs")
    path_logical  = st.secrets.get("PATH_SPECS_LOGICAL", "django/static/swagger/specs/logical_metadata")
    app_repo_name = st.secrets.get("APP_REPO_NAME", "")

    # GitHub auth -- a GitHub App installation token when its secrets are
    # configured, otherwise the existing PAT unchanged (see resolve_git_auth's
    # docstring). One installation is assumed to cover both the engineering
    # repo and this app's own repo; split into two resolve_git_auth() calls
    # with a second installation_id secret if that ever isn't true.
    github_app_id         = st.secrets.get("GITHUB_APP_ID", "")
    github_app_private_key = st.secrets.get("GITHUB_APP_PRIVATE_KEY", "")
    github_app_install_id  = st.secrets.get("GITHUB_APP_INSTALLATION_ID", "")

    git_user, git_token = resolve_git_auth(
        github_app_id, github_app_private_key, github_app_install_id,
        st.secrets.get("GIT_USER", ""), st.secrets.get("GIT_TOKEN", ""),
    )
    _, svc_git_token = resolve_git_auth(
        github_app_id, github_app_private_key, github_app_install_id,
        "", st.secrets.get("SVC_GIT_TOKEN", ""),
    )

    workspace_dir = Path("./temp_eng_workspace")
    workspace_dir.mkdir(exist_ok=True)

    # --- Load slug mapping ---
    current_mapping, current_sha = {}, None
    if svc_git_token and app_repo_name:
        current_mapping, current_sha = load_slug_mapping(app_repo_name, svc_git_token)
    else:
        st.error("⚠️ Missing Service Account secrets! Cannot load or save slug mappings.")

    # --- Sidebar ---
    # ReadMe Version lives in each tab's own "Upload to ReadMe.io" section now,
    # not here -- it's an Upload-mode-only, ReadMe-specific concern (same as
    # the target slug and slug-mapping save), never needed for Validation or
    # for pulling specs. This sidebar is source-level config only.
    with st.sidebar:
        st.header("⚙️ Task Configuration")
        eng_branch = st.text_input("Engineering Branch", value="master")
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
                if patch_known_upstream_yaml_bugs(workspace_dir, path_main):
                    st.info(
                        "🩹 Applied temporary workaround for a known YAML indentation "
                        "bug in `common/responses.yaml` (fails strict rdme validation). "
                        "Remove `patch_known_upstream_yaml_bugs` once the upstream fix "
                        "merges into the engineering repo."
                    )
                root_files = []
                for sub in [path_main, path_logical]:
                    tp = workspace_dir / sub
                    if tp.exists():
                        root_files.extend(tp.glob("*.yaml"))
                missing_refs = find_missing_ref_targets(root_files, workspace_dir)
                if missing_refs:
                    st.warning(
                        f"⚠️ Heads up: the following `$ref` targets are referenced by specs "
                        f"in this clone of branch `{eng_branch}` but don't exist in it. Any "
                        "spec that depends on one of these will fail validation with an "
                        "ENOENT error until it's fixed on that branch:\n\n"
                        + "\n".join(f"- `{m}`" for m in missing_refs)
                    )
            else:
                st.error(f"❌ Error: {p.stderr.decode()}")

    st.divider()
    npx = shutil.which("npx")
    tab_git, tab_manual = st.tabs([
        "🐙 Git Repo Pipeline",
        "📂 Manual File Upload",
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
        elif npx is None:
            st.error(
                "❌ `npx` was not found on PATH, so validation/upload commands can't run. "
                "This usually means Node.js failed to install — try clicking 'Reboot app' "
                "(Streamlit Cloud → Manage app) to force a clean environment setup."
            )
        else:
            try:
                selected_file_name = st.selectbox("Select Spec", file_options)
                selected_file_path = next(f for f in yaml_files if f.name == selected_file_name)
                st.caption(f"`{selected_file_name}`")

                st.divider()
                st.subheader("🔍 3. Validation")
                st.caption(
                    "Checks the spec as engineering wrote it, against any/all of these four "
                    "CLIs -- no ReadMe.io account, version, or slug involved at all."
                )
                validator_choice = st.multiselect(
                    "Validators", VALIDATOR_OPTIONS, default=DEFAULT_VALIDATORS, key="validator_choice_git",
                )
                if st.button("🔍 Run Validation"):
                    prepped = prep_openapi_file(selected_file_path, selected_file_path.stem, workspace_dir)
                    abs_cwd = str(prepped.parent.resolve())
                    st.write("### 🔍 Logs")
                    results = {}
                    if "swagger-cli" in validator_choice:
                        results["swagger-cli"] = run_command_ui(f"{npx} --yes swagger-cli validate {prepped.name}", cwd=abs_cwd)
                    if "rdme" in validator_choice:
                        results["rdme"] = run_command_ui(f"{npx} --yes rdme openapi validate {prepped.name}", cwd=abs_cwd)
                    if "Redocly (lint)" in validator_choice:
                        results["Redocly"] = run_command_ui(f"{npx} --yes @redocly/cli lint {prepped.name}", cwd=abs_cwd)
                    if "Mintlify (mint validate)" in validator_choice:
                        results["Mintlify"] = run_mintlify_validation(prepped)
                    if results:
                        st.write("### Summary")
                        for name, code in results.items():
                            (st.success if code == 0 else st.error)(f"{'✅' if code == 0 else '❌'} {name}: {'passed' if code == 0 else 'failed'}")
                    else:
                        st.warning("⚠️ No validators selected.")

                st.divider()
                st.subheader("☁️ 4. Upload to ReadMe.io")
                st.caption("ReadMe.io-only -- always runs swagger-cli (advisory) + rdme (required gate) before uploading.")
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

                if is_new_file:
                    st.caption(f"⚠️ Auto-generated slug (not yet in the mapping): `{mapped_id}`")
                elif mapped_id:
                    st.caption(f"✅ Mapped slug: `{mapped_id}`")
                final_id       = st.text_input("Target ReadMe Slug:", value=mapped_id)
                target_version = st.text_input("ReadMe Version", value=st.session_state.get("readme_version_git", "2026.5.0-0"), key="readme_version_git")

                if st.button("☁️ Upload to ReadMe", type="primary"):
                    if not final_id.strip():
                        st.error("❌ Target ReadMe Slug cannot be empty.")
                    else:
                        prepped = prep_openapi_file(selected_file_path, final_id, workspace_dir, version=target_version)
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
                        else:
                            st.error("❌ rdme validation failed -- ReadMe requires this to pass before uploading. See logs above.")
            except Exception as e:
                st.error("❌ Something failed while rendering the spec-selection/action panel below:")
                st.exception(e)

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

                st.divider()
                st.subheader("🔍 Validation")
                st.caption(
                    "Checks the spec as uploaded, against any/all of these four CLIs -- "
                    "no ReadMe.io account, version, or slug involved at all."
                )
                validator_choice_manual = st.multiselect(
                    "Validators", VALIDATOR_OPTIONS, default=DEFAULT_VALIDATORS, key="validator_choice_manual",
                )
                if st.button("🔍 Run Validation", key="run_validation_manual"):
                    manual_prepped = prep_openapi_file(manual_path, manual_path.stem, workspace_dir)
                    abs_cwd        = str(manual_prepped.parent.resolve())
                    st.write("### 🔍 Logs")
                    results = {}
                    if "swagger-cli" in validator_choice_manual:
                        results["swagger-cli"] = run_command_ui(f"{npx} --yes swagger-cli validate {manual_prepped.name}", cwd=abs_cwd)
                    if "rdme" in validator_choice_manual:
                        results["rdme"] = run_command_ui(f"{npx} --yes rdme openapi validate {manual_prepped.name}", cwd=abs_cwd)
                    if "Redocly (lint)" in validator_choice_manual:
                        results["Redocly"] = run_command_ui(f"{npx} --yes @redocly/cli lint {manual_prepped.name}", cwd=abs_cwd)
                    if "Mintlify (mint validate)" in validator_choice_manual:
                        results["Mintlify"] = run_mintlify_validation(manual_prepped)
                    if results:
                        st.write("### Summary")
                        for name, code in results.items():
                            (st.success if code == 0 else st.error)(f"{'✅' if code == 0 else '❌'} {name}: {'passed' if code == 0 else 'failed'}")
                    else:
                        st.warning("⚠️ No validators selected.")

                st.divider()
                st.subheader("☁️ Upload to ReadMe.io")
                st.caption("ReadMe.io-only -- always runs swagger-cli (advisory) + rdme (required gate) before uploading.")
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

                if is_manual_new:
                    st.caption(f"⚠️ Auto-generated slug (not yet in the mapping): `{manual_mapped_id}`")
                else:
                    st.caption(f"✅ Mapped slug: `{manual_mapped_id}`")
                manual_final_id      = st.text_input("Target ReadMe Slug:", value=manual_mapped_id, key="manual_slug_input")
                manual_target_version = st.text_input("ReadMe Version", value=st.session_state.get("readme_version_manual", "2026.5.0-0"), key="readme_version_manual")

                if st.button("☁️ Upload to ReadMe", type="primary", key="upload_manual"):
                    if not manual_final_id.strip():
                        st.error("❌ Target ReadMe Slug cannot be empty.")
                    else:
                        manual_prepped = prep_openapi_file(manual_path, manual_final_id, workspace_dir, version=manual_target_version)
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
                                f"--key {readme_key} --slug {manual_final_id}.json --branch {manual_target_version}"
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
                        else:
                            st.error("❌ rdme validation failed -- ReadMe requires this to pass before uploading. See logs above.")


if __name__ == "__main__":
    main()
