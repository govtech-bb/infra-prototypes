# Upload Pre-Flight + Destroy Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a preflight check to `deploy_infrastructure` (rejects unbuildable uploads, supports `home.html`-style entries) and add chat-driven destroy via two new tools (`list_deployments`, two-phase `destroy_infrastructure`).

**Architecture:** All new logic lives in `deploy-agent/tools.py`. The agent loop in `agent.py` gains one new branch (clear `session.deployment` after a successful destroy) and three system-prompt bullets. CLI symmetry: one new `scripts/list_deployments.py` + a `make destroy-list` target. No infra changes (the `index_document` tofu variable already exists).

**Tech Stack:** Python 3.11+, Anthropic SDK, OpenTofu, FastAPI, SQLite (stdlib), pytest + moto, ruff.

**Reference spec:** `docs/superpowers/specs/2026-05-05-upload-preflight-design.md`

---

## Conventions

- Working dir: `cd "/Users/christophercorbin/INFRA prototypes"` (note the space, quote it).
- Tests run from `deploy-agent/`: `cd deploy-agent && python3 -m pytest tests/`.
- Each task ends with a single commit. Commit messages use Conventional Commits.
- After each task, run `make check` from `deploy-agent/` and confirm green before committing.
- All paths shown are absolute or relative to the repo root.

---

## Task 1: Pre-flight upload check + custom entry filename

**Files:**
- Modify: `deploy-agent/tools.py`
- Modify: `deploy-agent/agent.py` (system prompt only)
- Modify: `deploy-agent/tests/test_tools.py`

- [ ] **Step 1.1: Write failing tests for `_preflight_uploads`**

Append to `deploy-agent/tests/test_tools.py`:

```python
# ── _preflight_uploads tests ──────────────────────────────────────────────────


def test_preflight_empty_upload(tmp_path):
    upload_dir = tmp_path / "empty"
    upload_dir.mkdir()
    err, idx = tools._preflight_uploads(str(upload_dir))
    assert err is not None
    assert "No files uploaded" in err["summary"]
    assert idx is None


def test_preflight_missing_dir():
    err, idx = tools._preflight_uploads(None)
    assert err is not None
    assert "No files uploaded" in err["summary"]
    assert idx is None


def test_preflight_jsx_with_no_html(tmp_path):
    (tmp_path / "App.jsx").write_text("export default () => null")
    (tmp_path / "index.css").write_text("body{}")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is not None
    assert "source code" in err["summary"].lower()
    assert "npm run build" in err["summary"]
    assert "App.jsx" in err["details"]
    assert idx is None


def test_preflight_single_non_index_html(tmp_path):
    (tmp_path / "home.html").write_text("<html></html>")
    (tmp_path / "style.css").write_text("body{}")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is None
    assert idx == "home.html"


def test_preflight_single_html_case_insensitive(tmp_path):
    (tmp_path / "Home.HTML").write_text("<html></html>")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is None
    assert idx == "Home.HTML"


def test_preflight_multiple_html_no_index(tmp_path):
    (tmp_path / "home.html").write_text("<html></html>")
    (tmp_path / "about.html").write_text("<html></html>")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is not None
    assert "Multiple HTML files" in err["summary"]
    assert "home.html" in err["details"]
    assert "about.html" in err["details"]
    assert idx is None


def test_preflight_index_html_present(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "style.css").write_text("body{}")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is None
    assert idx is None  # caller defaults to "index.html"


def test_preflight_index_html_with_source_files_passes(tmp_path):
    # If an index.html is present, source files alongside it are fine
    # (e.g., a built site that bundled .ts source maps).
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "app.ts").write_text("export {}")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is None
    assert idx is None
```

- [ ] **Step 1.2: Run tests, confirm they fail**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k preflight
```
Expected: 8 failures, all `AttributeError: module 'tools' has no attribute '_preflight_uploads'`.

- [ ] **Step 1.3: Add `_preflight_uploads` to `tools.py`**

In `deploy-agent/tools.py`, AFTER `_classify_error` and BEFORE `# ── Tool Definitions ───`, add:

```python
# ── Upload preflight ──────────────────────────────────────────────────────────

_SOURCE_EXTENSIONS = re.compile(r"\.(jsx|tsx|ts|vue|svelte|scss|sass|less)$", re.IGNORECASE)


def _preflight_uploads(upload_dir: str | None) -> tuple[dict | None, str | None]:
    """Inspect uploaded files before deploy.

    Returns (error_dict, index_document):
      - error_dict: {"summary", "details"} if deploy should be blocked, else None.
      - index_document: filename to use as the homepage, or None (caller defaults
        to "index.html"). Only set when auto-detected from a single non-index HTML.
    """
    if not upload_dir or not Path(upload_dir).exists():
        return (
            {"summary": "No files uploaded yet — drag a folder into the chat first.", "details": ""},
            None,
        )

    files = [p for p in Path(upload_dir).rglob("*") if p.is_file()]
    if not files:
        return (
            {"summary": "No files uploaded yet — drag a folder into the chat first.", "details": ""},
            None,
        )

    html_files = [p for p in files if p.suffix.lower() in (".html", ".htm")]
    source_files = [p for p in files if _SOURCE_EXTENSIONS.search(p.name)]

    if source_files and not html_files:
        sample = ", ".join(p.name for p in source_files[:10])
        return (
            {
                "summary": (
                    "Looks like source code, not a built site. Run 'npm run build' "
                    "(or your project's build command) and upload the output folder "
                    "(usually 'dist/' or 'build/')."
                ),
                "details": f"Found: {sample}",
            },
            None,
        )

    has_index = any(p.name.lower() == "index.html" for p in html_files)
    if has_index:
        return (None, None)

    if len(html_files) == 1:
        # Auto-select the single HTML file as the entry document.
        return (None, html_files[0].name)

    if len(html_files) > 1:
        sample = ", ".join(p.name for p in html_files[:10])
        return (
            {
                "summary": (
                    "Multiple HTML files but no index.html. Tell me which one is the "
                    "homepage (e.g., 'use home.html as the entry')."
                ),
                "details": f"Found: {sample}",
            },
            None,
        )

    # No HTML at all and no source files — let it through; tofu will create
    # the bucket and the user will see the empty-bucket error if any.
    return (None, None)
```

- [ ] **Step 1.4: Run preflight tests, confirm they pass**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k preflight
```
Expected: 8 passed.

- [ ] **Step 1.5: Write the deploy short-circuit + index_document tests**

Append to `deploy-agent/tests/test_tools.py`:

```python
# ── deploy_infrastructure preflight integration ───────────────────────────────


@patch("tools.subprocess.run")
def test_deploy_short_circuits_on_preflight_fail(mock_run, tmp_path):
    # Empty upload_dir → preflight blocks deploy, no subprocess call.
    upload_dir = tmp_path / "empty"
    upload_dir.mkdir()
    from sessions import Session
    session = Session(session_id="s", upload_dir=str(upload_dir))

    result = tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
        session=session,
    )

    assert "summary" in result
    assert "No files uploaded" in result["summary"]
    mock_run.assert_not_called()


@patch("tools.subprocess.run")
def test_deploy_passes_index_document_var_when_auto_detected(mock_run, tmp_path):
    # Single non-index HTML → preflight auto-selects it; tofu apply receives
    # -var=index_document=home.html.
    (tmp_path / "home.html").write_text("<html></html>")
    (tmp_path / "style.css").write_text("body{}")
    from sessions import Session
    session = Session(session_id="s", upload_dir=str(tmp_path))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "output":
            return MagicMock(returncode=0, stdout='''{
              "bucket_name": {"value": "x-proto-static"},
              "site_url": {"value": "https://d.cloudfront.net"},
              "cloudfront_distribution_id": {"value": "ABC"}
            }''', stderr="")
        return MagicMock(returncode=0, stderr="", stdout="")
    mock_run.side_effect = fake_run

    result = tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
        session=session,
    )
    assert "summary" not in result
    apply_calls = [c for c in mock_run.call_args_list if c.args[0][1] == "apply"]
    assert apply_calls, "tofu apply was not called"
    apply_cmd = apply_calls[0].args[0]
    assert "-var=index_document=home.html" in apply_cmd


@patch("tools.subprocess.run")
def test_deploy_passes_explicit_index_document_overrides_auto(mock_run, tmp_path):
    # Caller-provided index_document wins over auto-detection.
    (tmp_path / "home.html").write_text("<html></html>")
    from sessions import Session
    session = Session(session_id="s", upload_dir=str(tmp_path))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "output":
            return MagicMock(returncode=0, stdout='''{
              "bucket_name": {"value": "x"},
              "site_url": {"value": "https://x"},
              "cloudfront_distribution_id": {"value": "C"}
            }''', stderr="")
        return MagicMock(returncode=0, stderr="", stdout="")
    mock_run.side_effect = fake_run

    tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
        index_document="custom.html",
        session=session,
    )
    apply_calls = [c for c in mock_run.call_args_list if c.args[0][1] == "apply"]
    apply_cmd = apply_calls[0].args[0]
    assert "-var=index_document=custom.html" in apply_cmd


@patch("tools.subprocess.run")
def test_deploy_default_index_html_when_present(mock_run, tmp_path):
    # index.html present → no -var=index_document= flag (default applies).
    (tmp_path / "index.html").write_text("<html></html>")
    from sessions import Session
    session = Session(session_id="s", upload_dir=str(tmp_path))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "output":
            return MagicMock(returncode=0, stdout='''{
              "bucket_name": {"value": "x"},
              "site_url": {"value": "https://x"},
              "cloudfront_distribution_id": {"value": "C"}
            }''', stderr="")
        return MagicMock(returncode=0, stderr="", stdout="")
    mock_run.side_effect = fake_run

    tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
        session=session,
    )
    apply_calls = [c for c in mock_run.call_args_list if c.args[0][1] == "apply"]
    apply_cmd = apply_calls[0].args[0]
    assert not any(arg.startswith("-var=index_document=") for arg in apply_cmd), \
        f"Unexpected index_document flag in {apply_cmd}"
```

- [ ] **Step 1.6: Run new deploy tests, confirm they fail**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k "deploy_short_circuits or deploy_passes_index or deploy_passes_explicit or deploy_default_index"
```
Expected: 4 failures (preflight not yet wired into `deploy_infrastructure`; `deploy_infrastructure` doesn't take `session` or `index_document` parameters yet, and existing happy-path test calls don't pass session).

Note: `test_deploy_infrastructure_happy_path_returns_outputs`, `test_deploy_infrastructure_init_failure_returns_summary`, and `test_deploy_infrastructure_apply_failure_returns_summary` will start failing in step 1.7 because the function signature is changing. We'll update those tests in step 1.8.

- [ ] **Step 1.7: Wire preflight into `deploy_infrastructure` and add `index_document` parameter**

In `deploy-agent/tools.py`, modify `deploy_infrastructure`. Find the function signature:

```python
def deploy_infrastructure(
    project_name: str,
    env: str,
    site_title: str,
    owner_name: str,
    owner_email: str,
    is_spa: bool = False,
    **_
) -> dict:
```

Replace with:

```python
def deploy_infrastructure(
    project_name: str,
    env: str,
    site_title: str,
    owner_name: str,
    owner_email: str,
    is_spa: bool = False,
    index_document: str | None = None,
    *,
    session=None,
    **_
) -> dict:
```

Right after the function's docstring, BEFORE the `try:` block, insert:

```python
    upload_dir = session.upload_dir if session is not None else None
    preflight_error, auto_index = _preflight_uploads(upload_dir)
    if preflight_error is not None:
        return preflight_error
    chosen_index = index_document or auto_index
```

Inside the `try:` block, find the `tofu apply` invocation:

```python
        # 3. Apply
        r = subprocess.run(
            [
                "tofu", "apply", "-auto-approve", "-input=false",
                f"-var=project_name={project_name}",
                f"-var=env={env}",
                f"-var=site_title={site_title}",
                f"-var=owner_name={owner_name}",
                f"-var=owner_email={owner_email}",
                f"-var=is_spa={'true' if is_spa else 'false'}",
            ],
            cwd=STACK_DIR, capture_output=True, text=True, timeout=600
        )
```

Replace with:

```python
        # 3. Apply
        apply_cmd = [
            "tofu", "apply", "-auto-approve", "-input=false",
            f"-var=project_name={project_name}",
            f"-var=env={env}",
            f"-var=site_title={site_title}",
            f"-var=owner_name={owner_name}",
            f"-var=owner_email={owner_email}",
            f"-var=is_spa={'true' if is_spa else 'false'}",
        ]
        if chosen_index:
            apply_cmd.append(f"-var=index_document={chosen_index}")
        r = subprocess.run(
            apply_cmd,
            cwd=STACK_DIR, capture_output=True, text=True, timeout=600
        )
```

Update the `TOOL_DEFINITIONS` entry for `deploy_infrastructure`. Find:

```python
                "is_spa": {
                    "type": "boolean",
                    "description": "True if this is a single-page app (React, Vue, etc.) that needs 404→index.html routing."
                }
            },
```

Replace with:

```python
                "is_spa": {
                    "type": "boolean",
                    "description": "True if this is a single-page app (React, Vue, etc.) that needs 404→index.html routing."
                },
                "index_document": {
                    "type": "string",
                    "description": "Filename to use as the website's homepage (e.g., 'index.html', 'home.html'). Optional — auto-detected from uploaded files when there is exactly one HTML file. Defaults to 'index.html'."
                }
            },
```

Update `execute_tool` to forward `session`. Find:

```python
def execute_tool(name: str, inputs: dict, session_id: str, session) -> dict:
    """Dispatch a tool call from the Claude agent."""
    if name == "deploy_infrastructure":
        return deploy_infrastructure(**inputs)
    elif name == "upload_files":
        return upload_files(session=session, **inputs)
    return {"summary": f"Unknown tool: {name}", "details": ""}
```

Replace with:

```python
def execute_tool(name: str, inputs: dict, session_id: str, session) -> dict:
    """Dispatch a tool call from the Claude agent."""
    if name == "deploy_infrastructure":
        return deploy_infrastructure(session=session, **inputs)
    elif name == "upload_files":
        return upload_files(session=session, **inputs)
    return {"summary": f"Unknown tool: {name}", "details": ""}
```

- [ ] **Step 1.8: Update the three existing deploy tests to pass `session`**

In `deploy-agent/tests/test_tools.py`, find `test_deploy_infrastructure_init_failure_returns_summary`. The current call:

```python
    result = tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
    )
```

Add a session with a valid upload_dir so preflight passes (we want the init failure to be the failure mode under test, not preflight). Modify the test signature and add a fixture-like setup:

```python
@patch("tools.subprocess.run")
def test_deploy_infrastructure_init_failure_returns_summary(mock_run, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    from sessions import Session
    session = Session(session_id="s", upload_dir=str(tmp_path))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "init":
            return MagicMock(returncode=1, stderr="NoCredentialProviders: ...")
        raise AssertionError(f"Unexpected subprocess call after init failure: {cmd}")
    mock_run.side_effect = fake_run

    result = tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
        session=session,
    )
    assert "summary" in result
    assert "details" in result
    assert "credentials" in result["summary"].lower()
```

Same fixup for `test_deploy_infrastructure_apply_failure_returns_summary`:

```python
@patch("tools.subprocess.run")
def test_deploy_infrastructure_apply_failure_returns_summary(mock_run, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    from sessions import Session
    session = Session(session_id="s", upload_dir=str(tmp_path))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "apply":
            return MagicMock(returncode=1, stderr="AccessDenied: not authorized to s3:CreateBucket")
        return MagicMock(returncode=0, stderr="", stdout="{}")
    mock_run.side_effect = fake_run

    result = tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
        session=session,
    )
    assert "summary" in result
    assert "permission" in result["summary"].lower() or "credentials" in result["summary"].lower()
```

Same for `test_deploy_infrastructure_happy_path_returns_outputs`:

```python
@patch("tools.subprocess.run")
def test_deploy_infrastructure_happy_path_returns_outputs(mock_run, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    from sessions import Session
    session = Session(session_id="s", upload_dir=str(tmp_path))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "output":
            return MagicMock(returncode=0, stdout='''{
              "bucket_name": {"value": "x-proto-static"},
              "site_url": {"value": "https://d.cloudfront.net"},
              "cloudfront_distribution_id": {"value": "ABC"}
            }''', stderr="")
        return MagicMock(returncode=0, stderr="", stdout="")
    mock_run.side_effect = fake_run

    result = tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
        session=session,
    )
    assert "summary" not in result
    assert result["bucket_name"] == "x-proto-static"
    assert result["site_url"] == "https://d.cloudfront.net"
    assert result["project_name"] == "x"
    assert result["env"] == "proto"
```

- [ ] **Step 1.9: Run all tool tests, confirm pass**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v
```
Expected: all preflight tests + 3 fixed-up deploy tests + 4 new deploy tests + the existing classify/upload tests all pass.

- [ ] **Step 1.10: Update system prompt in `agent.py`**

In `deploy-agent/agent.py`, find the `Rules:` section in `SYSTEM_PROMPT`. Find the line:

```
- Never ask for AWS credentials — assume they're configured in the environment.
```

Insert a new bullet ABOVE it:

```
- If files are uploaded but no `index.html` is present, ask the user which file should be the homepage instead of guessing — only auto-select when there is exactly one HTML file.
- Never ask for AWS credentials — assume they're configured in the environment.
```

- [ ] **Step 1.11: Run full test suite + ruff + tofu validate**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && make check 2>&1 | tail -10
```
Expected: `All checks passed!`, all tests pass, `Success! The configuration is valid.`

- [ ] **Step 1.12: Commit**

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git add deploy-agent/tools.py deploy-agent/agent.py deploy-agent/tests/test_tools.py
git commit -m "feat: pre-flight upload check + custom entry filename

- _preflight_uploads helper inspects upload_dir before tofu init
- Empty / source-only uploads are rejected with a friendly summary
- Single non-index HTML auto-selects as index_document
- Multiple HTML files with no index.html prompts the user
- deploy_infrastructure gains optional index_document parameter,
  threaded through tofu via -var=index_document=
- 12 new tests in test_tools.py"
```

---

## Task 2: `list_deployments` tool + `make destroy-list` CLI

**Files:**
- Modify: `deploy-agent/tools.py` (add `list_deployments` tool + helper)
- Modify: `deploy-agent/tests/test_tools.py`
- Create: `scripts/list_deployments.py`
- Modify: `deploy-agent/Makefile`

- [ ] **Step 2.1: Write failing tests for `list_deployments`**

Append to `deploy-agent/tests/test_tools.py`:

```python
# ── list_deployments tests ────────────────────────────────────────────────────


def _seed_session(db_path, project_name, env, deployment):
    """Helper: write a session row directly to a SQLite file."""
    import sqlite3
    import json as _json
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
          session_id   TEXT PRIMARY KEY,
          messages     TEXT NOT NULL DEFAULT '[]',
          files        TEXT NOT NULL DEFAULT '[]',
          upload_dir   TEXT,
          deployment   TEXT,
          project_name TEXT,
          env          TEXT,
          created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO sessions (session_id, deployment, project_name, env) "
        "VALUES (?, ?, ?, ?)",
        (f"sess-{project_name}-{env}", _json.dumps(deployment), project_name, env),
    )
    conn.commit()
    conn.close()


@patch("tools.subprocess.run")
def test_list_deployments_filters_to_active(mock_run, tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    _seed_session(db_path, "alpha", "proto", {
        "site_title": "Alpha", "owner_name": "A", "site_url": "https://a",
        "project_name": "alpha", "env": "proto",
    })
    _seed_session(db_path, "beta", "proto", {
        "site_title": "Beta", "owner_name": "B", "site_url": "https://b",
        "project_name": "beta", "env": "proto",
    })
    _seed_session(db_path, "gamma", "proto", {
        "site_title": "Gamma", "owner_name": "G", "site_url": "https://g",
        "project_name": "gamma", "env": "proto",
    })
    monkeypatch.setenv("DEPLOY_AGENT_DB", str(db_path))

    # tofu workspace list reports only alpha-proto and gamma-proto remain.
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="  default\n* alpha-proto\n  gamma-proto\n",
        stderr="",
    )

    result = tools.list_deployments(session=None)
    assert "deployments" in result
    names = {d["project_name"] for d in result["deployments"]}
    assert names == {"alpha", "gamma"}


def test_list_deployments_empty_when_no_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_AGENT_DB", str(tmp_path / "nope.db"))
    result = tools.list_deployments(session=None)
    assert result == {"deployments": []}


@patch("tools.subprocess.run")
def test_list_deployments_empty_when_no_deployed_sessions(mock_run, tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    # Create the table but no rows with deployment.
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
          session_id TEXT PRIMARY KEY, deployment TEXT, project_name TEXT, env TEXT
        )
    """)
    conn.commit()
    conn.close()
    monkeypatch.setenv("DEPLOY_AGENT_DB", str(db_path))

    result = tools.list_deployments(session=None)
    assert result == {"deployments": []}
    mock_run.assert_not_called()  # Don't shell out to tofu when there are no rows.
```

- [ ] **Step 2.2: Run new tests, confirm they fail**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k list_deployments
```
Expected: 3 failures, `AttributeError: module 'tools' has no attribute 'list_deployments'`.

- [ ] **Step 2.3: Add `list_deployments` to `tools.py`**

In `deploy-agent/tools.py`, ADD `import sqlite3` to the top imports if not present (should already be at the top from earlier). It is. Add `import os` if not present (it is).

Add a new helper near the top, AFTER the `_classify_error` block, BEFORE the `# ── Upload preflight ───` block (or after preflight — order doesn't matter):

```python
# ── Deployment listing ────────────────────────────────────────────────────────


def _read_active_deployments() -> list[dict]:
    """Read sessions.db and intersect with current tofu workspaces."""
    db_path = Path(os.environ.get(
        "DEPLOY_AGENT_DB",
        str(Path(__file__).parent / "data" / "sessions.db"),
    ))
    if not db_path.exists():
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT project_name, env, deployment, updated_at FROM sessions "
        "WHERE deployment IS NOT NULL AND project_name IS NOT NULL"
    ).fetchall()
    conn.close()
    if not rows:
        return []

    # Intersect with tofu workspace list to drop destroyed-but-still-recorded.
    ws = subprocess.run(
        ["tofu", "workspace", "list"],
        cwd=STACK_DIR, capture_output=True, text=True,
    )
    if ws.returncode != 0:
        # If tofu isn't available, fall back to all recorded deployments.
        active = {f"{r['project_name']}-{r['env']}" for r in rows}
    else:
        active = {
            line.lstrip("* ").strip()
            for line in ws.stdout.splitlines()
            if line.strip() and line.strip() != "default"
        }

    deployments = []
    for r in rows:
        ws_name = f"{r['project_name']}-{r['env']}"
        if ws_name not in active:
            continue
        try:
            dep = json.loads(r["deployment"])
        except (json.JSONDecodeError, TypeError):
            dep = {}
        deployments.append({
            "project_name": r["project_name"],
            "env":          r["env"],
            "site_title":   dep.get("site_title", ""),
            "owner_name":   dep.get("owner_name", ""),
            "site_url":     dep.get("site_url", ""),
            "updated_at":   r["updated_at"],
        })
    return deployments


def list_deployments(*, session=None, **_) -> dict:
    """Tool: return every active static-site deployment."""
    try:
        return {"deployments": _read_active_deployments()}
    except Exception as e:
        return {"summary": "Could not list deployments.", "details": str(e)}
```

Add the tool definition. Find the end of the `TOOL_DEFINITIONS` list (after the `upload_files` entry's closing `}`):

```python
    {
        "name": "upload_files",
        ...
        "required": ["bucket_name", "distribution_id"]
        }
    }
]
```

Add a new entry between `upload_files` and the closing `]`:

```python
    {
        "name": "upload_files",
        ...
    },
    {
        "name": "list_deployments",
        "description": (
            "List every active static-site deployment recorded for this user. "
            "Read-only — no infrastructure changes. Use before destroy_infrastructure "
            "when the user's request is ambiguous (e.g., 'destroy my old test site')."
        ),
        "input_schema": {"type": "object", "properties": {}}
    }
]
```

Wire it into `execute_tool`. Find:

```python
def execute_tool(name: str, inputs: dict, session_id: str, session) -> dict:
    """Dispatch a tool call from the Claude agent."""
    if name == "deploy_infrastructure":
        return deploy_infrastructure(session=session, **inputs)
    elif name == "upload_files":
        return upload_files(session=session, **inputs)
    return {"summary": f"Unknown tool: {name}", "details": ""}
```

Replace with:

```python
def execute_tool(name: str, inputs: dict, session_id: str, session) -> dict:
    """Dispatch a tool call from the Claude agent."""
    if name == "deploy_infrastructure":
        return deploy_infrastructure(session=session, **inputs)
    elif name == "upload_files":
        return upload_files(session=session, **inputs)
    elif name == "list_deployments":
        return list_deployments(session=session, **inputs)
    return {"summary": f"Unknown tool: {name}", "details": ""}
```

- [ ] **Step 2.4: Run list tests, confirm they pass**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k list_deployments
```
Expected: 3 passed.

- [ ] **Step 2.5: Create `scripts/list_deployments.py`**

```python
#!/usr/bin/env python3
"""Print every active static-site deployment recorded in sessions.db.

Active = recorded in sessions.db AND tofu workspace still exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make tools.py importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "deploy-agent"))

from tools import _read_active_deployments  # noqa: E402


def main() -> int:
    deployments = _read_active_deployments()
    if not deployments:
        print("No active deployments.")
        return 0

    print(f"{'project':<25} {'env':<10} {'site_title':<25} {'site_url'}")
    print("-" * 90)
    for d in deployments:
        print(
            f"{d['project_name']:<25} "
            f"{d['env']:<10} "
            f"{d['site_title']:<25.25} "
            f"{d['site_url']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Make it executable:

```bash
chmod +x "/Users/christophercorbin/INFRA prototypes/scripts/list_deployments.py"
```

- [ ] **Step 2.6: Add `make destroy-list` target**

In `deploy-agent/Makefile`, find:

```make
destroy-all:
	python3 ../scripts/destroy_all.py
```

Add ABOVE it:

```make
destroy-list:
	python3 ../scripts/list_deployments.py

```

(Use a blank line between targets. Recipe lines must be tab-indented.)

Update the `.PHONY:` line to include `destroy-list`:

Find:
```make
.PHONY: check lint format test infra-validate destroy-all install install-dev
```

Replace with:
```make
.PHONY: check lint format test infra-validate destroy-list destroy-all install install-dev
```

- [ ] **Step 2.7: Verify CLI script works**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && make destroy-list
```
Expected: `No active deployments.` (assuming the local sessions.db has no deployments — or the actual list if you've deployed previously).

- [ ] **Step 2.8: Run `make check`**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && make check 2>&1 | tail -5
```
Expected: green.

- [ ] **Step 2.9: Commit**

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git add deploy-agent/tools.py deploy-agent/tests/test_tools.py \
        deploy-agent/Makefile scripts/list_deployments.py
git commit -m "feat: list_deployments tool + make destroy-list CLI

- New read-only tool returns active deployments (sessions.db ∩ tofu workspaces)
- _read_active_deployments helper shared between tool and CLI script
- 3 new tests
- scripts/list_deployments.py prints the same data for human use
- make destroy-list wraps it"
```

---

## Task 3: `destroy_infrastructure` tool (two-phase)

**Files:**
- Modify: `deploy-agent/tools.py`
- Modify: `deploy-agent/agent.py` (post-destroy session clear + system prompt bullets)
- Modify: `deploy-agent/tests/test_tools.py`
- Modify: `deploy-agent/tests/test_agent.py`

- [ ] **Step 3.1: Write failing tests for `destroy_infrastructure`**

Append to `deploy-agent/tests/test_tools.py`:

```python
# ── destroy_infrastructure tests ──────────────────────────────────────────────


def test_destroy_unknown_project_returns_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_AGENT_DB", str(tmp_path / "nope.db"))
    from sessions import Session
    session = Session(session_id="s")

    result = tools.destroy_infrastructure(
        project_name="ghost", env="proto",
        session=session,
    )
    assert "summary" in result
    assert "No record" in result["summary"]
    assert result.get("preview") is not True


def test_destroy_preview_returns_preview_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    _seed_session(db_path, "alpha", "proto", {
        "site_title": "Alpha Site", "owner_name": "A. User",
        "site_url": "https://a.cloudfront.net",
        "bucket_name": "alpha-proto-static",
        "cloudfront_distribution_id": "ABC123",
        "project_name": "alpha", "env": "proto",
    })
    monkeypatch.setenv("DEPLOY_AGENT_DB", str(db_path))
    from sessions import Session
    session = Session(session_id="s")

    with patch("tools.subprocess.run") as mock_run:
        result = tools.destroy_infrastructure(
            project_name="alpha", env="proto",
            confirm=False,
            session=session,
        )

    assert result["preview"] is True
    assert "Will destroy" in result["message"]
    assert "alpha-proto" in result["message"]
    assert "summary" not in result
    assert "deployment" in result
    assert result["deployment"]["site_title"] == "Alpha Site"
    mock_run.assert_not_called()


@patch("tools.subprocess.run")
def test_destroy_confirm_runs_destroy_and_clears_session(mock_run, tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    _seed_session(db_path, "alpha", "proto", {
        "site_title": "Alpha", "owner_name": "A", "site_url": "https://a",
        "bucket_name": "alpha-proto-static", "cloudfront_distribution_id": "ABC",
        "project_name": "alpha", "env": "proto",
    })
    monkeypatch.setenv("DEPLOY_AGENT_DB", str(db_path))
    from sessions import Session
    session = Session(
        session_id="s",
        deployment={"project_name": "alpha", "env": "proto", "site_url": "https://a"},
    )

    # init OK, workspace select OK, destroy OK
    mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

    result = tools.destroy_infrastructure(
        project_name="alpha", env="proto",
        confirm=True,
        session=session,
    )
    assert result.get("destroyed") is True
    assert result["project_name"] == "alpha"
    assert result["env"] == "proto"
    # session.deployment cleared because (project_name, env) matched.
    assert session.deployment is None
    # tofu destroy was called with the right -var= flags.
    destroy_calls = [c for c in mock_run.call_args_list if c.args[0][1] == "destroy"]
    assert destroy_calls, "tofu destroy was not invoked"
    destroy_cmd = destroy_calls[0].args[0]
    assert "-var=project_name=alpha" in destroy_cmd
    assert "-var=env=proto" in destroy_cmd
    # tofu init was also called (idempotent on fresh checkouts).
    init_calls = [c for c in mock_run.call_args_list if c.args[0][1] == "init"]
    assert init_calls


@patch("tools.subprocess.run")
def test_destroy_workspace_already_gone_is_idempotent(mock_run, tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    _seed_session(db_path, "alpha", "proto", {
        "site_title": "Alpha", "project_name": "alpha", "env": "proto",
    })
    monkeypatch.setenv("DEPLOY_AGENT_DB", str(db_path))
    from sessions import Session
    session = Session(session_id="s")

    def fake_run(cmd, **kwargs):
        if cmd[1] == "init":
            return MagicMock(returncode=0, stderr="", stdout="")
        if cmd[:3] == ["tofu", "workspace", "select"]:
            return MagicMock(returncode=1, stderr="workspace alpha-proto does not exist", stdout="")
        raise AssertionError(f"Unexpected call: {cmd}")
    mock_run.side_effect = fake_run

    result = tools.destroy_infrastructure(
        project_name="alpha", env="proto",
        confirm=True,
        session=session,
    )
    # Idempotent destroy: workspace already gone is a success-shaped result.
    assert result.get("destroyed") is True
    assert "summary" not in result


@patch("tools.subprocess.run")
def test_destroy_failure_returns_classified_summary(mock_run, tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    _seed_session(db_path, "alpha", "proto", {
        "site_title": "Alpha", "project_name": "alpha", "env": "proto",
    })
    monkeypatch.setenv("DEPLOY_AGENT_DB", str(db_path))
    from sessions import Session
    session = Session(session_id="s")

    def fake_run(cmd, **kwargs):
        if cmd[1] == "destroy":
            return MagicMock(returncode=1, stderr="AccessDenied: not authorized", stdout="")
        return MagicMock(returncode=0, stderr="", stdout="")
    mock_run.side_effect = fake_run

    result = tools.destroy_infrastructure(
        project_name="alpha", env="proto",
        confirm=True,
        session=session,
    )
    assert "summary" in result
    assert "permission" in result["summary"].lower() or "credentials" in result["summary"].lower()
    assert result.get("destroyed") is not True
```

- [ ] **Step 3.2: Run new tests, confirm they fail**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k destroy
```
Expected: 5 failures, `AttributeError: module 'tools' has no attribute 'destroy_infrastructure'`.

- [ ] **Step 3.3: Add `destroy_infrastructure` to `tools.py`**

In `deploy-agent/tools.py`, ADD a new function. Place it AFTER `list_deployments` and the `_read_active_deployments` helper:

```python
# ── Destroy ───────────────────────────────────────────────────────────────────


def _find_deployment_record(project_name: str, env: str) -> dict | None:
    """Look up sessions.db for a deployment matching project_name + env."""
    db_path = Path(os.environ.get(
        "DEPLOY_AGENT_DB",
        str(Path(__file__).parent / "data" / "sessions.db"),
    ))
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT deployment FROM sessions "
        "WHERE project_name = ? AND env = ? AND deployment IS NOT NULL "
        "ORDER BY updated_at DESC LIMIT 1",
        (project_name, env),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    try:
        return json.loads(row["deployment"])
    except (json.JSONDecodeError, TypeError):
        return None


def destroy_infrastructure(
    project_name: str,
    env: str,
    confirm: bool = False,
    *,
    session=None,
    **_,
) -> dict:
    """Two-phase destroy. confirm=False returns a preview; confirm=True runs tofu destroy."""
    record = _find_deployment_record(project_name, env)
    if record is None:
        return {
            "summary": (
                f"No record of '{project_name}-{env}'. Run list_deployments first."
            ),
            "details": "",
        }

    if not confirm:
        bucket = record.get("bucket_name", "?")
        site = record.get("site_url", "?")
        owner = record.get("owner_name", "?")
        return {
            "preview": True,
            "message": (
                f"Will destroy {project_name}-{env} (bucket: {bucket}, site: {site}, "
                f"owner: {owner}). Reply 'yes destroy it' to proceed."
            ),
            "deployment": record,
        }

    # confirm=True: actually destroy
    try:
        # 1. init (idempotent — needed on fresh checkouts)
        r = subprocess.run(
            ["tofu", "init", "-input=false"],
            cwd=STACK_DIR, capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            return _classify_error(r.stderr)

        # 2. workspace select — if missing, treat destroy as already-done
        workspace = f"{project_name}-{env}"
        sel = subprocess.run(
            ["tofu", "workspace", "select", workspace],
            cwd=STACK_DIR, capture_output=True, text=True,
        )
        if sel.returncode != 0:
            _maybe_clear_session_deployment(session, project_name, env)
            return {
                "destroyed": True,
                "project_name": project_name,
                "env": env,
                "note": "Workspace was already gone — nothing to destroy.",
            }

        # 3. destroy
        r = subprocess.run(
            [
                "tofu", "destroy", "-auto-approve", "-input=false",
                f"-var=project_name={project_name}",
                f"-var=env={env}",
            ],
            cwd=STACK_DIR, capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            return _classify_error(r.stderr)

        _maybe_clear_session_deployment(session, project_name, env)
        return {"destroyed": True, "project_name": project_name, "env": env}

    except subprocess.TimeoutExpired:
        return {"summary": "Destroy timed out after 10 minutes.", "details": ""}
    except Exception as e:
        return {"summary": "Destroy failed unexpectedly.", "details": str(e)}


def _maybe_clear_session_deployment(session, project_name: str, env: str) -> None:
    """If the current session's deployment matches the destroyed (project, env), clear it."""
    if session is None or session.deployment is None:
        return
    if (
        session.deployment.get("project_name") == project_name
        and session.deployment.get("env") == env
    ):
        session.deployment = None
```

Add the tool definition. In `TOOL_DEFINITIONS`, after the `list_deployments` entry, add:

```python
    },
    {
        "name": "list_deployments",
        ...
    },
    {
        "name": "destroy_infrastructure",
        "description": (
            "Destroy a static-site deployment. Two-phase: first call with confirm=false "
            "to preview, then call again with confirm=true after the user explicitly "
            "confirms. Always cite the exact project_name and env from list_deployments "
            "or session.deployment — never guess."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Exact project_name from list_deployments / session.deployment."
                },
                "env": {
                    "type": "string",
                    "description": "Exact env from list_deployments / session.deployment."
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Set to true to actually destroy. Default false returns a preview only."
                }
            },
            "required": ["project_name", "env"]
        }
    }
]
```

Wire it into `execute_tool`. Find:

```python
    elif name == "list_deployments":
        return list_deployments(session=session, **inputs)
    return {"summary": f"Unknown tool: {name}", "details": ""}
```

Replace with:

```python
    elif name == "list_deployments":
        return list_deployments(session=session, **inputs)
    elif name == "destroy_infrastructure":
        return destroy_infrastructure(session=session, **inputs)
    return {"summary": f"Unknown tool: {name}", "details": ""}
```

- [ ] **Step 3.4: Run destroy tests, confirm they pass**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k destroy
```
Expected: 5 passed.

- [ ] **Step 3.5: Update system prompt in `agent.py`**

In `deploy-agent/agent.py`, find the `Rules:` section. The line:

```
- If files are uploaded but no `index.html` is present, ask the user which file should be the homepage instead of guessing — only auto-select when there is exactly one HTML file.
```

INSERT below it two new bullets (preserving the order of existing bullets):

```
- If files are uploaded but no `index.html` is present, ask the user which file should be the homepage instead of guessing — only auto-select when there is exactly one HTML file.
- To destroy a deployment, always pass the exact `project_name` and `env`. If the user says "destroy this" / "destroy it", use the values from the most recent successful deploy. If they name a different site, call `list_deployments` first and ask the user to pick — never fuzzy-match.
- Destroy is two-phase: call `destroy_infrastructure` with `confirm=false` first. The result will have `preview: true` and a `message` field — relay that message verbatim to the user and ask them to confirm. Only call again with `confirm=true` after the user explicitly confirms in their next reply.
```

Also update the workflow numbered list to mention destroy. Find:

```
Your deployment workflow:
1. Greet the user briefly and ask what they'd like to deploy.
```

Replace the heading with:

```
Your workflow:

When the user wants to **deploy**:
1. Greet the user briefly and ask what they'd like to deploy.
```

And after step 7 (the existing last step), add:

```
   "✅ Your site is live! → https://d1234.cloudfront.net"

When the user wants to **destroy** a deployment:
1. If they're referring to the site they just deployed in this chat, you can call destroy_infrastructure directly with confirm=false using the project_name and env from the latest deployment.
2. If they reference a different site by name, call list_deployments first; show them the candidates by site_title; let them pick.
3. Call destroy_infrastructure with confirm=false. Surface the preview message verbatim and ask "are you sure?".
4. On their explicit confirmation ("yes", "destroy it", "go ahead"), call destroy_infrastructure with the same project_name + env and confirm=true.
5. Report success: "✓ Destroyed <project_name>-<env>." On failure, surface the summary.

Rules:
```

(Note the preserved `Rules:` heading after the new section.)

- [ ] **Step 3.6: Add a post-destroy guard in the agent loop**

The current `agent.py` already has the post-deploy cache logic. After a successful destroy, the tool already clears `session.deployment` via `_maybe_clear_session_deployment` — no agent-loop change is strictly needed because the tool mutates `session` directly.

However, we should add a test in `test_agent.py` to lock in the contract that the agent loop does NOT cache a destroy result as a deployment. Append to `deploy-agent/tests/test_agent.py`:

```python
def test_destroy_result_does_not_get_cached_as_deployment(monkeypatch):
    client = MagicMock()
    destroy_block = _block("tool_use", id="t1", name="destroy_infrastructure", input={})
    client.messages.create.side_effect = [
        _response([destroy_block], "tool_use"),
        _response([_block("text", text="Destroyed.")], "end_turn"),
    ]
    monkeypatch.setattr(
        agent, "execute_tool",
        lambda name, inputs, session_id, session: {
            "destroyed": True, "project_name": "x", "env": "proto",
        },
    )

    session = Session(session_id="s1")
    agent.run_agent_loop(client, session)
    # Destroy result must NOT populate session.deployment.
    assert session.deployment is None
```

- [ ] **Step 3.7: Run all tests + ruff + tofu**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && make check 2>&1 | tail -10
```
Expected: green, all tests pass.

- [ ] **Step 3.8: Commit**

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git add deploy-agent/tools.py deploy-agent/agent.py \
        deploy-agent/tests/test_tools.py deploy-agent/tests/test_agent.py
git commit -m "feat: chat-driven destroy with two-phase confirm

- destroy_infrastructure tool with required project_name+env, optional confirm
- confirm=false returns {preview: true, message, deployment}; no AWS calls
- confirm=true runs tofu init -> workspace select -> destroy
- Workspace-already-gone treated as idempotent success
- session.deployment cleared after successful destroy if it matches
- System prompt: never fuzzy-match, always two-phase, surface preview verbatim
- 5 destroy tests + 1 agent-loop guard test"
```

---

## Task 4: Push and verify CI

**Files:** none.

- [ ] **Step 4.1: Push to GitHub**

```bash
cd "/Users/christophercorbin/INFRA prototypes" && git push 2>&1 | tail -5
```
Expected: 3 new commits pushed to `origin/main`.

- [ ] **Step 4.2: Verify CI runs green**

```bash
sleep 8 && gh -R christophercorbin/infra-prototypes run list --limit 1
```
Wait for the run to start. Then:

```bash
gh -R christophercorbin/infra-prototypes run watch <run-id> --exit-status 2>&1 | tail -10
```
Expected: success.

- [ ] **Step 4.3: Manual smoke (optional)**

If you want to exercise the new flow against real AWS:
```bash
cd "/Users/christophercorbin/INFRA prototypes"
# Start the agent
cd deploy-agent && ./run.sh
# In a different terminal, drag examples/sample-site/ into the chat,
# deploy, then say "destroy it" and confirm.
```

---

## Self-review

**Spec coverage:**
- ✅ `_preflight_uploads` helper with all 4 rules → Task 1, steps 1.3
- ✅ `index_document` parameter on `deploy_infrastructure` → Task 1, step 1.7
- ✅ System prompt bullet for missing-index-html guidance → Task 1, step 1.10
- ✅ `list_deployments` tool with sessions.db ∩ tofu workspaces → Task 2, step 2.3
- ✅ `make destroy-list` CLI symmetry → Task 2, steps 2.5–2.6
- ✅ `destroy_infrastructure` two-phase tool → Task 3, step 3.3
- ✅ Preview shape `{preview, message, deployment}` (no `summary`) → Task 3, step 3.3
- ✅ Workspace-already-gone idempotent path → Task 3, step 3.3 + test 3.1
- ✅ session.deployment cleared post-destroy when matching → `_maybe_clear_session_deployment` in step 3.3
- ✅ System prompt destroy bullets + workflow update → Task 3, step 3.5
- ✅ All 14 spec-mandated tests + 1 agent-loop guard test = 15 new tests
- ✅ Three-commit build sequence (preflight / list / destroy) → Tasks 1, 2, 3

**Placeholder scan:** No TBDs, TODOs, "implement later", or hand-wavy steps.

**Type consistency:** `_preflight_uploads` returns `tuple[dict | None, str | None]` everywhere. `_read_active_deployments` returns `list[dict]`. `destroy_infrastructure` accepts `(project_name, env, confirm, *, session)` consistently across signature, schema, tests. `_maybe_clear_session_deployment` signature is `(session, project_name, env)` — used once.

**Files touched per task:**
- Task 1: `tools.py`, `agent.py`, `tests/test_tools.py`
- Task 2: `tools.py`, `tests/test_tools.py`, `Makefile`, new `scripts/list_deployments.py`
- Task 3: `tools.py`, `agent.py`, `tests/test_tools.py`, `tests/test_agent.py`
- Task 4: none (push + verify only)

No file is touched in incompatible ways across tasks.

---

## Done criteria

- `make check` passes locally and CI passes on push.
- The agent in a real chat:
  - Refuses to deploy a folder of `.jsx` source code with a friendly summary.
  - Auto-detects `home.html` and deploys it as the homepage.
  - Lists active deployments when asked.
  - Previews then destroys when the user says "destroy this" → confirms → "yes."
- `make destroy-list` from `deploy-agent/` prints the same data.
- All 15 new tests pass.
