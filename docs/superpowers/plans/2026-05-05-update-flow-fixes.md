# Update Flow Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unblock chat-driven updates by surfacing bucket/CF IDs in `list_deployments`, re-injecting newly uploaded files into chat context, catching `index (N).html` browser-duplicate downloads in preflight, and adding an "update" workflow to the system prompt.

**Architecture:** Single small commit on top of `main`. Touches `tools.py`, `app.py`, `agent.py`, `sessions.py`, and adds tests in `test_tools.py`, `test_app.py`, `test_sessions.py`. Adds one column to the `sessions` SQLite table with a defensive `ALTER TABLE` migration so existing local DBs don't break.

**Tech Stack:** Python 3.11+, FastAPI, SQLite (stdlib), pytest, ruff, OpenTofu.

**Reference spec:** `docs/superpowers/specs/2026-05-05-update-flow-fixes-design.md`

---

## Conventions

- Working dir: `cd "/Users/christophercorbin/INFRA prototypes"` (note the space, quote it).
- Tests run from `deploy-agent/`: `cd deploy-agent && python3 -m pytest tests/`.
- Single commit at the end of all four fixes; intermediate verification with `make check`.
- All paths absolute or relative to the repo root.

---

## Task 1: Schema migration + Session field + SqliteSessionStore round-trip

**Files:**
- Modify: `deploy-agent/sessions.py`
- Modify: `deploy-agent/tests/test_sessions.py`

- [ ] **Step 1.1: Write failing test for `last_injected_file_count` round-trip**

Append to `deploy-agent/tests/test_sessions.py`:

```python
def test_last_injected_file_count_round_trips(store):
    s = store.create()
    assert s.last_injected_file_count == 0  # default
    s.last_injected_file_count = 3
    store.save(s)

    loaded = store.get(s.session_id)
    assert loaded.last_injected_file_count == 3


def test_alter_table_idempotent_on_existing_db(tmp_path):
    # First store creates the table with the new column.
    SqliteSessionStore(tmp_path / "sessions.db")
    # Second store on the same DB should not error on the migration.
    store2 = SqliteSessionStore(tmp_path / "sessions.db")
    s = store2.create()
    s.last_injected_file_count = 5
    store2.save(s)
    assert store2.get(s.session_id).last_injected_file_count == 5
```

- [ ] **Step 1.2: Run tests, confirm they fail**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_sessions.py -v -k "last_injected or alter_table"
```
Expected: 2 failures, `AttributeError: 'Session' object has no attribute 'last_injected_file_count'`.

- [ ] **Step 1.3: Add `last_injected_file_count` to `Session` dataclass**

In `deploy-agent/sessions.py`, find:

```python
@dataclass
class Session:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    upload_dir: str | None = None
    deployment: dict | None = None
```

Replace with:

```python
@dataclass
class Session:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    upload_dir: str | None = None
    deployment: dict | None = None
    last_injected_file_count: int = 0
```

- [ ] **Step 1.4: Add column to `_SCHEMA` and migration to `SqliteSessionStore.__init__`**

In `deploy-agent/sessions.py`, find `_SCHEMA`:

```python
_SCHEMA = """
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
);
"""
```

Replace with:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id               TEXT PRIMARY KEY,
  messages                 TEXT NOT NULL DEFAULT '[]',
  files                    TEXT NOT NULL DEFAULT '[]',
  upload_dir               TEXT,
  deployment               TEXT,
  project_name             TEXT,
  env                      TEXT,
  last_injected_file_count INTEGER NOT NULL DEFAULT 0,
  created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
```

In `SqliteSessionStore.__init__`, find:

```python
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)
```

Replace with:

```python
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            # Defensive migration: add the column if upgrading from an older DB.
            try:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN "
                    "last_injected_file_count INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                # Column already exists — fresh schema or already migrated.
                pass
```

- [ ] **Step 1.5: Update `SqliteSessionStore.get` to read the column**

In `deploy-agent/sessions.py`, find the `get` method:

```python
    def get(self, session_id: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        deployment = json.loads(row["deployment"]) if row["deployment"] else None
        return Session(
            session_id=row["session_id"],
            messages=json.loads(row["messages"]),
            files=json.loads(row["files"]),
            upload_dir=row["upload_dir"],
            deployment=deployment,
        )
```

Replace with:

```python
    def get(self, session_id: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        deployment = json.loads(row["deployment"]) if row["deployment"] else None
        return Session(
            session_id=row["session_id"],
            messages=json.loads(row["messages"]),
            files=json.loads(row["files"]),
            upload_dir=row["upload_dir"],
            deployment=deployment,
            last_injected_file_count=row["last_injected_file_count"] or 0,
        )
```

- [ ] **Step 1.6: Update `SqliteSessionStore.save` to write the column**

In `deploy-agent/sessions.py`, find the `save` method's INSERT statement:

```python
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, messages, files, upload_dir,
                    deployment, project_name, env, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO UPDATE SET
                    messages    = excluded.messages,
                    files       = excluded.files,
                    upload_dir  = excluded.upload_dir,
                    deployment  = excluded.deployment,
                    project_name= excluded.project_name,
                    env         = excluded.env,
                    updated_at  = CURRENT_TIMESTAMP
                """,
                (
                    session.session_id,
                    json.dumps(session.messages),
                    json.dumps(session.files),
                    session.upload_dir,
                    deployment_json,
                    project_name,
                    env,
                ),
            )
```

Replace with:

```python
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, messages, files, upload_dir,
                    deployment, project_name, env,
                    last_injected_file_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO UPDATE SET
                    messages                 = excluded.messages,
                    files                    = excluded.files,
                    upload_dir               = excluded.upload_dir,
                    deployment               = excluded.deployment,
                    project_name             = excluded.project_name,
                    env                      = excluded.env,
                    last_injected_file_count = excluded.last_injected_file_count,
                    updated_at               = CURRENT_TIMESTAMP
                """,
                (
                    session.session_id,
                    json.dumps(session.messages),
                    json.dumps(session.files),
                    session.upload_dir,
                    deployment_json,
                    project_name,
                    env,
                    session.last_injected_file_count,
                ),
            )
```

- [ ] **Step 1.7: Run tests, confirm pass**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_sessions.py -v
```
Expected: 8 passed (6 prior + 2 new).

---

## Task 2: `app.py` re-injects newly uploaded files

**Files:**
- Modify: `deploy-agent/app.py`
- Modify: `deploy-agent/tests/test_app.py`

- [ ] **Step 2.1: Write failing tests**

Append to `deploy-agent/tests/test_app.py`:

```python
def test_chat_injects_uploaded_files_on_first_turn(client, monkeypatch):
    # Stub run_agent_loop so we can inspect what was injected without a real LLM.
    captured = {}

    def fake_loop(_client, session):
        captured["last_user"] = session.messages[-1]
        return "ok"

    import app as app_module
    monkeypatch.setattr(app_module, "run_agent_loop", fake_loop)

    session = _make_session(client)
    client.post(
        f"/api/upload/{session}",
        files=[("files", ("index.html", io.BytesIO(b"<html></html>"), "text/html"))],
    )
    client.post(
        "/api/chat",
        json={"session_id": session, "message": "deploy"},
    )

    assert captured["last_user"]["role"] == "user"
    assert "[Uploaded files: index.html]" in captured["last_user"]["content"]


def test_chat_re_injects_newly_uploaded_files(client, monkeypatch):
    captured_turns = []

    def fake_loop(_client, session):
        captured_turns.append(session.messages[-1]["content"])
        return "ok"

    import app as app_module
    monkeypatch.setattr(app_module, "run_agent_loop", fake_loop)

    session = _make_session(client)

    # First upload + chat: should inject [Uploaded files: ...]
    client.post(
        f"/api/upload/{session}",
        files=[("files", ("index.html", io.BytesIO(b"<html></html>"), "text/html"))],
    )
    client.post("/api/chat", json={"session_id": session, "message": "first"})

    # Second upload + chat: should inject [Newly uploaded: ...] for the new file only
    client.post(
        f"/api/upload/{session}",
        files=[("files", ("style.css", io.BytesIO(b"body{}"), "text/css"))],
    )
    client.post("/api/chat", json={"session_id": session, "message": "second"})

    assert "[Uploaded files: index.html]" in captured_turns[0]
    assert "[Newly uploaded: style.css]" in captured_turns[1]
    # Second turn must NOT repeat the first file.
    assert "index.html" not in captured_turns[1].split("[Newly")[1]


def test_chat_no_injection_when_files_unchanged(client, monkeypatch):
    captured_turns = []

    def fake_loop(_client, session):
        captured_turns.append(session.messages[-1]["content"])
        return "ok"

    import app as app_module
    monkeypatch.setattr(app_module, "run_agent_loop", fake_loop)

    session = _make_session(client)
    client.post(
        f"/api/upload/{session}",
        files=[("files", ("index.html", io.BytesIO(b"<html></html>"), "text/html"))],
    )
    client.post("/api/chat", json={"session_id": session, "message": "first"})
    # No new upload — second message should be plain.
    client.post("/api/chat", json={"session_id": session, "message": "second"})

    assert "[Newly uploaded:" not in captured_turns[1]
    assert "[Uploaded files:" not in captured_turns[1]
    assert captured_turns[1] == "second"
```

- [ ] **Step 2.2: Run tests, confirm they fail**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_app.py -v -k "chat_injects or chat_re_injects or chat_no_injection"
```
Expected: at least one failure (the second test fails because re-injection isn't implemented; the first might pass because the legacy "[Uploaded files:" injection still fires).

- [ ] **Step 2.3: Replace the chat handler's injection logic**

In `deploy-agent/app.py`, find the `chat` function:

```python
@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    session = _get_or_404(req.session_id)

    user_content = req.message
    already_injected = any(
        "[Uploaded files:" in str(m.get("content", ""))
        for m in session.messages
        if isinstance(m, dict)
    )
    if session.files and not already_injected:
        file_list = ", ".join(session.files)
        user_content = f"{req.message}\n\n[Uploaded files: {file_list}]"

    session.messages.append({"role": "user", "content": user_content})
    reply = run_agent_loop(client, session)
    store.save(session)

    return ChatResponse(message=reply, deployment=session.deployment)
```

Replace with:

```python
@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    session = _get_or_404(req.session_id)

    user_content = req.message
    last_announced = session.last_injected_file_count
    current_count = len(session.files)
    if current_count > last_announced:
        new_files = session.files[last_announced:]
        file_list = ", ".join(new_files)
        if last_announced == 0:
            user_content = f"{req.message}\n\n[Uploaded files: {file_list}]"
        else:
            user_content = f"{req.message}\n\n[Newly uploaded: {file_list}]"
        session.last_injected_file_count = current_count

    session.messages.append({"role": "user", "content": user_content})
    reply = run_agent_loop(client, session)
    store.save(session)

    return ChatResponse(message=reply, deployment=session.deployment)
```

- [ ] **Step 2.4: Run tests, confirm pass**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_app.py -v
```
Expected: all tests pass (the new 3 + 6 prior = 9).

---

## Task 3: `list_deployments` returns bucket + CF distribution

**Files:**
- Modify: `deploy-agent/tools.py`
- Modify: `deploy-agent/tests/test_tools.py`
- Modify: `scripts/list_deployments.py`

- [ ] **Step 3.1: Write failing test**

Append to `deploy-agent/tests/test_tools.py`:

```python
@patch("tools.subprocess.run")
def test_list_deployments_returns_bucket_and_cf(mock_run, tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    _seed_session(db_path, "alpha", "proto", {
        "site_title": "Alpha", "owner_name": "A",
        "site_url": "https://a.cloudfront.net",
        "bucket_name": "alpha-proto-static",
        "cloudfront_distribution_id": "EABC123XYZ",
        "project_name": "alpha", "env": "proto",
    })
    monkeypatch.setenv("DEPLOY_AGENT_DB", str(db_path))

    mock_run.return_value = MagicMock(
        returncode=0, stdout="* alpha-proto\n", stderr="",
    )

    result = tools.list_deployments(session=None)
    assert len(result["deployments"]) == 1
    d = result["deployments"][0]
    assert d["bucket_name"] == "alpha-proto-static"
    assert d["cloudfront_distribution_id"] == "EABC123XYZ"
```

- [ ] **Step 3.2: Run test, confirm it fails**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k "list_deployments_returns_bucket"
```
Expected: KeyError or assertion failure on `bucket_name` not in result.

- [ ] **Step 3.3: Update `_read_active_deployments`**

In `deploy-agent/tools.py`, find:

```python
        deployments.append({
            "project_name": r["project_name"],
            "env":          r["env"],
            "site_title":   dep.get("site_title", ""),
            "owner_name":   dep.get("owner_name", ""),
            "site_url":     dep.get("site_url", ""),
            "updated_at":   r["updated_at"],
        })
```

Replace with:

```python
        deployments.append({
            "project_name":               r["project_name"],
            "env":                        r["env"],
            "site_title":                 dep.get("site_title", ""),
            "owner_name":                 dep.get("owner_name", ""),
            "site_url":                   dep.get("site_url", ""),
            "bucket_name":                dep.get("bucket_name", ""),
            "cloudfront_distribution_id": dep.get("cloudfront_distribution_id", ""),
            "updated_at":                 r["updated_at"],
        })
```

- [ ] **Step 3.4: Update CLI script to show bucket column**

In `scripts/list_deployments.py`, find:

```python
    print(f"{'project':<25} {'env':<10} {'site_title':<25} {'site_url'}")
    print("-" * 90)
    for d in deployments:
        print(
            f"{d['project_name']:<25} "
            f"{d['env']:<10} "
            f"{d['site_title']:<25.25} "
            f"{d['site_url']}"
        )
```

Replace with:

```python
    print(f"{'project':<25} {'env':<10} {'bucket':<30} {'site_url'}")
    print("-" * 110)
    for d in deployments:
        print(
            f"{d['project_name']:<25} "
            f"{d['env']:<10} "
            f"{d['bucket_name']:<30.30} "
            f"{d['site_url']}"
        )
```

- [ ] **Step 3.5: Run tests**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k list_deployments
```
Expected: 4 passing (3 prior + 1 new). The 3 prior tests (`filters_to_active`, `empty_when_no_db`, `empty_when_no_deployed_sessions`) should still pass — they don't assert on the absence of new keys.

---

## Task 4: Preflight catches `index (N).html` duplicate-download pattern

**Files:**
- Modify: `deploy-agent/tools.py`
- Modify: `deploy-agent/tests/test_tools.py`

- [ ] **Step 4.1: Write failing tests**

Append to `deploy-agent/tests/test_tools.py`:

```python
def test_preflight_rejects_duplicate_index(tmp_path):
    (tmp_path / "index (1).html").write_text("<html></html>")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is not None
    assert "browser-duplicate" in err["summary"].lower() or "(1)" in err["summary"]
    assert "rename" in err["summary"].lower()
    assert idx is None


def test_preflight_rejects_duplicate_index_case_variants(tmp_path):
    # Case-insensitive: Index, INDEX, mixed; .htm and .html; with and without space.
    for name in ["Index (2).HTML", "index(1).htm", "INDEX  (3).html"]:
        d = tmp_path / name.replace("/", "_").replace(" ", "_space_")
        d.mkdir()
        (d / name).write_text("<html></html>")
        err, _ = tools._preflight_uploads(str(d))
        assert err is not None, f"failed for {name!r}"
        assert "rename" in err["summary"].lower(), f"failed for {name!r}"


def test_preflight_allows_legitimate_index_html(tmp_path):
    # Sanity: plain index.html should still pass through.
    (tmp_path / "index.html").write_text("<html></html>")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is None
    assert idx is None
```

- [ ] **Step 4.2: Run tests, confirm they fail**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k "preflight_rejects_duplicate or preflight_allows_legitimate"
```
Expected: 2 failures on the duplicate tests; 1 pass on the legitimate test.

- [ ] **Step 4.3: Add the duplicate-download check in `_preflight_uploads`**

In `deploy-agent/tools.py`, find:

```python
_SOURCE_EXTENSIONS = re.compile(r"\.(jsx|tsx|ts|vue|svelte|scss|sass|less)$", re.IGNORECASE)
```

Add directly below it:

```python
_DUPLICATE_DOWNLOAD = re.compile(r"^index\s*\(\d+\)\.html?$", re.IGNORECASE)
```

In the `_preflight_uploads` function body, find the empty-files-list check:

```python
    files = [p for p in Path(upload_dir).rglob("*") if p.is_file()]
    if not files:
        return (
            {"summary": "No files uploaded yet — drag a folder into the chat first.", "details": ""},
            None,
        )
```

Insert the new check directly AFTER it, BEFORE the `html_files = ...` line:

```python
    duplicate_index = next(
        (p for p in files if _DUPLICATE_DOWNLOAD.match(p.name)),
        None,
    )
    if duplicate_index is not None:
        return (
            {
                "summary": (
                    f"Your homepage is named '{duplicate_index.name}' — looks like a "
                    "browser-duplicate download. Rename it to 'index.html' and upload again."
                ),
                "details": "",
            },
            None,
        )
```

- [ ] **Step 4.4: Run tests, confirm pass**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k "preflight"
```
Expected: all preflight tests pass.

---

## Task 5: System prompt — "update" workflow + Rules bullet

**Files:**
- Modify: `deploy-agent/agent.py`

- [ ] **Step 5.1: Add the update workflow section**

In `deploy-agent/agent.py`, find the destroy workflow section's last line (step 5):

```
5. Report success: "✓ Destroyed <project_name>-<env>." On failure, surface the summary.

Rules:
```

Replace with:

```
5. Report success: "✓ Destroyed <project_name>-<env>." On failure, surface the summary.

When the user wants to **update** an existing deployment (push new files to a site that's already live):
1. Identify which deployment by name. If they say "this" or "the one I just deployed", use `session.deployment`. Otherwise, call `list_deployments` and ask them to pick.
2. From the matched record, take the `bucket_name` and `cloudfront_distribution_id` — never guess them.
3. If they haven't uploaded files yet, ask them to upload.
4. Once files are uploaded (you'll see `[Newly uploaded: ...]` in their next message), call `upload_files` with the bucket + distribution from step 2.
5. Report success with the live URL. CloudFront cache invalidation already runs as part of upload_files.

Rules:
```

- [ ] **Step 5.2: Add the update-flow Rules bullet**

In `deploy-agent/agent.py`, find the existing two destroy bullets (the second one ending with "in their next reply."):

```
- Destroy is two-phase: call `destroy_infrastructure` with `confirm=false` first. The result will have `preview: true` and a `message` field — relay that message verbatim to the user and ask them to confirm. Only call again with `confirm=true` after the user explicitly confirms in their next reply.
- Never ask for AWS credentials — assume they're configured in the environment.
```

Insert a new bullet between those two:

```
- Destroy is two-phase: call `destroy_infrastructure` with `confirm=false` first. The result will have `preview: true` and a `message` field — relay that message verbatim to the user and ask them to confirm. Only call again with `confirm=true` after the user explicitly confirms in their next reply.
- For updates, use upload_files directly with the existing bucket_name + cloudfront_distribution_id from list_deployments. Don't call deploy_infrastructure again — the infra is already there.
- Never ask for AWS credentials — assume they're configured in the environment.
```

---

## Task 6: Run `make check` + commit + push + verify CI

**Files:** none.

- [ ] **Step 6.1: Run `make check`**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && make check 2>&1 | tail -10
```
Expected: green. All previous + new tests pass (~58 total). ruff clean. tofu validate clean.

If ruff complains, run `make format` and re-check; include any auto-format diffs in the commit.

- [ ] **Step 6.2: Commit**

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git add deploy-agent/sessions.py deploy-agent/app.py deploy-agent/agent.py \
        deploy-agent/tools.py deploy-agent/tests/ scripts/list_deployments.py
git commit -m "feat: chat-driven update flow + duplicate-download preflight + file re-injection

Surfaced from a real chat-driven update attempt:
- list_deployments returns bucket_name + cloudfront_distribution_id
- app.py chat handler tracks last_injected_file_count and re-injects only
  newly uploaded files as [Newly uploaded: ...]
- Preflight rejects index (N).html browser-duplicate downloads
- System prompt: When the user wants to **update** workflow + Rules bullet
  steering the agent to upload_files (not redeploy) for updates
- Defensive ALTER TABLE for last_injected_file_count column

7 new tests across test_sessions, test_app, test_tools."
```

Do NOT add Co-Authored-By lines.

- [ ] **Step 6.3: Push**

```bash
cd "/Users/christophercorbin/INFRA prototypes" && git push 2>&1 | tail -3
```
Expected: 1 commit pushed.

- [ ] **Step 6.4: Watch CI**

```bash
sleep 8 && gh -R christophercorbin/infra-prototypes run list --limit 1 --json databaseId,status,conclusion -q '.[]'
```
Then:
```bash
gh -R christophercorbin/infra-prototypes run watch <databaseId> --exit-status 2>&1 | tail -3
```
Expected: success.

---

## Self-review

**Spec coverage:**
- ✅ `list_deployments` exposes `bucket_name` + `cloudfront_distribution_id` → Task 3 step 3.3
- ✅ `app.py` re-injects newly uploaded files via `last_injected_file_count` → Task 1 + Task 2
- ✅ Preflight catches `index (N).html` → Task 4
- ✅ System prompt update workflow + Rules bullet → Task 5
- ✅ Schema migration via defensive `ALTER TABLE` → Task 1 step 1.4
- ✅ All 7 spec-mandated tests covered

**Placeholder scan:** No TBDs / TODOs / hand-wavy steps.

**Type consistency:** `last_injected_file_count` is `int` in dataclass, schema, get/save, and chat handler. `_DUPLICATE_DOWNLOAD` regex literal matches between code and tests.

**Files touched per task:**
- Task 1: `sessions.py`, `tests/test_sessions.py`
- Task 2: `app.py`, `tests/test_app.py`
- Task 3: `tools.py`, `tests/test_tools.py`, `scripts/list_deployments.py`
- Task 4: `tools.py`, `tests/test_tools.py`
- Task 5: `agent.py`
- Task 6: none (commit/push/verify)

No file is touched in incompatible ways across tasks.

---

## Done criteria

- `make check` green locally and in CI.
- The agent in a real chat:
  - Lists deployments showing bucket + CF distribution ID.
  - Updates a site by uploading files and saying "update X" — no follow-up "what's the bucket name?" turn.
  - Sees mid-chat re-uploads as `[Newly uploaded: ...]` annotations.
  - Refuses `index (1).html` with a rename-and-reupload message.
- 7 new tests pass; no regressions.
