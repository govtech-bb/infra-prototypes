# Deploy Agent Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing local prototype in this directory into a public GitHub repo with persistent sessions, automated tests, manual smoke test, fixed upload path bug, friendlier tool errors, and CI.

**Architecture:** Light refactor — extract `agent.py` and `sessions.py` from the existing `app.py`, replace the in-memory session dict with a SQLite-backed `SessionStore`, fix the upload path-stripping bug, add a `_classify_error` helper to `tools.py`, scaffold `examples/sample-site/`, `scripts/smoke-test.sh`, `scripts/destroy_all.py`, GitHub Actions CI, and a README. Existing OpenTofu modules are unchanged.

**Tech Stack:** Python 3.11+, FastAPI, Anthropic SDK (`claude-opus-4-6`), SQLite (stdlib), pytest + moto, ruff, OpenTofu, AWS (S3 + CloudFront), GitHub Actions.

**Reference spec:** `docs/superpowers/specs/2026-05-05-deploy-agent-hardening-design.md`

---

## Conventions

- All commands assume the repo root is your working directory: `cd "/Users/christophercorbin/INFRA prototypes"`. The folder name contains a space — quote paths in shell.
- Each task ends with a commit. Commit messages use Conventional Commits (`feat:`, `fix:`, `test:`, `chore:`, `docs:`).
- Python code targets 3.11 (already required by `anthropic` SDK; FastAPI annotations use `|` union syntax).
- Tests in `deploy-agent/tests/` run from the `deploy-agent/` directory: `cd deploy-agent && pytest tests/`.

---

## Task 1: Initialize git repo and skeleton

**Files:**
- Create: `.gitignore`
- Create: `README.md` (skeleton only — final pass is Task 10)
- Modify: nothing else (existing files commit as the baseline)

- [ ] **Step 1.1: Confirm we are not already in a git repo**

Run from repo root:
```bash
git rev-parse --is-inside-work-tree 2>&1 || echo "NOT A REPO"
```
Expected: `NOT A REPO`. If it prints `true`, stop and ask before continuing.

- [ ] **Step 1.2: `git init` with a `main` branch**

Run:
```bash
git init -b main
```
Expected: `Initialized empty Git repository in …/INFRA prototypes/.git/`.

- [ ] **Step 1.3: Write `.gitignore`**

Create `.gitignore` at the repo root with this exact content:

```gitignore
# Python
__pycache__/
*.pyc
*.pyo
.venv/
venv/
.pytest_cache/
.ruff_cache/
.mypy_cache/

# OpenTofu / Terraform
.terraform/
.terraform.lock.hcl
*.tfstate
*.tfstate.*
*.tfvars
crash.log
crash.*.log

# Agent runtime
deploy-agent/data/
/tmp/deploy-sessions/

# Editors
.vscode/
.idea/
*.swp

# OS
.DS_Store
Thumbs.db
```

- [ ] **Step 1.4: Write `README.md` skeleton**

Create `README.md` at the repo root with this exact content (the body sections are placeholders that Task 10 fills in):

```markdown
# INFRA Deploy Agent

Chat with an AI agent to deploy a static website to AWS (S3 + CloudFront).

## Status

🚧 Hardening in progress. See `docs/superpowers/plans/2026-05-05-deploy-agent-hardening.md`.

## Quickstart

_Filled in at the end of the hardening pass._

## Architecture

_Filled in at the end of the hardening pass._

## Project layout

```
deploy-agent/   FastAPI chat UI + Claude agent loop
infra/          OpenTofu modules and stacks
examples/       Sample static site (drag into the chat to try it)
scripts/        Smoke test and destroy-all helper
docs/           Design docs and implementation plans
```

## License

MIT (add a LICENSE file before publishing the repo if desired).
```

- [ ] **Step 1.5: First commit**

Run:
```bash
git add .gitignore README.md CLAUDE.md "deploy-agent" "infra" "docs"
git status
```
Expected: all the existing prototype files + the two new ones are staged.

```bash
git commit -m "chore: initialize repo with existing prototype as baseline"
```
Expected: a commit with ~25 files added.

- [ ] **Step 1.6: Sanity-check the app still runs**

Run (in a new terminal so you can ctrl-C):
```bash
cd deploy-agent
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" ./run.sh
```
Expected: server starts, prints `Open http://localhost:8000`. Open it in a browser and confirm the chat UI renders. Ctrl-C to stop.

---

## Task 2: Light refactor — extract `agent.py` and `sessions.py` (in-memory)

This task moves code without changing behavior. We introduce the `SessionStore` interface with an in-memory implementation; Task 3 swaps it for SQLite.

**Files:**
- Create: `deploy-agent/sessions.py`
- Create: `deploy-agent/agent.py`
- Modify: `deploy-agent/app.py` (slim down to route handlers + DI)

- [ ] **Step 2.1: Create `deploy-agent/sessions.py`**

```python
"""Session model + storage interface.

Task 2 ships an in-memory implementation. Task 3 replaces it with a SQLite
implementation behind the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4


@dataclass
class Session:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    upload_dir: str | None = None
    deployment: dict | None = None


class SessionStore(Protocol):
    def create(self) -> Session: ...
    def get(self, session_id: str) -> Session | None: ...
    def save(self, session: Session) -> None: ...


class InMemorySessionStore:
    """Drop-in replacement for the original `sessions: dict` in app.py."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        session = Session(session_id=str(uuid4()))
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def save(self, session: Session) -> None:
        self._sessions[session.session_id] = session
```

- [ ] **Step 2.2: Create `deploy-agent/agent.py`**

```python
"""Claude tool-use loop and system prompt.

Extracted from app.py without behavior changes (other than serializing
ContentBlock objects to dicts for forward compatibility with the SQLite store).
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from sessions import Session
from tools import TOOL_DEFINITIONS, execute_tool

MAX_AGENT_ITERATIONS = 15

SYSTEM_PROMPT = """You are the INFRA Deploy Agent — a friendly assistant that deploys \
static websites to AWS (S3 + CloudFront) on behalf of the user.

Your deployment workflow:
1. Greet the user briefly and ask what they'd like to deploy.
2. If files have been uploaded (you'll see them listed in the message), acknowledge them.
3. Collect the following through natural conversation — only ask for what you don't have:
   - Site title / name  (e.g. "My Portfolio", "Acme Landing Page")
   - Owner's full name
   - Owner's email address
   - Whether it's a single-page app (React, Vue, etc.)
4. Confirm the details in a short summary, then proceed — don't ask for confirmation twice.
5. Call deploy_infrastructure to provision S3 + CloudFront.
6. Call upload_files to push their files live.
7. Return the live URL clearly, e.g.:
   "✅ Your site is live! → https://d1234.cloudfront.net"

Rules:
- Be concise. One question at a time.
- Derive project_name from the site title (lowercase slug, hyphens, max 20 chars).
- Use env="proto" for all prototype deployments unless the user says otherwise.
- If a tool returns an error, explain it simply and suggest what to check.
- Never ask for AWS credentials — assume they're configured in the environment.
"""


def _serialize_content(blocks: list[Any]) -> list[dict]:
    """Convert Anthropic ContentBlock objects to JSON-serializable dicts."""
    return [b.model_dump() for b in blocks]


def run_agent_loop(client: anthropic.Anthropic, session: Session) -> str:
    """Run the Claude agentic loop until a final text response is produced."""
    for _ in range(MAX_AGENT_ITERATIONS):
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=session.messages,
        )

        session.messages.append(
            {"role": "assistant", "content": _serialize_content(response.content)}
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                result = execute_tool(
                    block.name,
                    block.input,
                    session_id=session.session_id,
                    session=session,
                )

                if block.name == "deploy_infrastructure" and "summary" not in result:
                    session.deployment = result

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

            session.messages.append({"role": "user", "content": tool_results})

    return "The deployment agent reached its iteration limit. Please try again."
```

- [ ] **Step 2.3: Update `tools.py` to read from `Session` dataclass instead of `dict`**

In `deploy-agent/tools.py`, change `upload_files` and `execute_tool` to use attribute access on `session`. Find the function `upload_files` and replace its first lines:

Old:
```python
def upload_files(
    bucket_name: str,
    distribution_id: str,
    session: dict,
    **_
) -> dict:
    """Upload files from the session's temp directory to S3, then invalidate CloudFront."""
    upload_dir = session.get("upload_dir")
    if not upload_dir or not Path(upload_dir).exists():
```

New:
```python
def upload_files(
    bucket_name: str,
    distribution_id: str,
    session,
    **_
) -> dict:
    """Upload files from the session's temp directory to S3, then invalidate CloudFront."""
    upload_dir = session.upload_dir
    if not upload_dir or not Path(upload_dir).exists():
```

The `execute_tool` signature can stay; only the type comment changes. No other edits to `tools.py` in this task.

- [ ] **Step 2.4: Rewrite `deploy-agent/app.py`**

Replace the entire contents of `deploy-agent/app.py` with:

```python
"""INFRA Deploy Agent — FastAPI routes."""

from __future__ import annotations

from pathlib import Path
from typing import List

import anthropic
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import run_agent_loop
from sessions import InMemorySessionStore, Session

app = FastAPI(title="INFRA Deploy Agent")
client = anthropic.Anthropic()
store = InMemorySessionStore()


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    message: str
    deployment: dict | None = None


def _get_or_404(session_id: str) -> Session:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(404, f"Unknown session_id: {session_id}")
    return session


@app.get("/api/session")
def new_session() -> dict:
    session = store.create()
    return {"session_id": session.session_id}


@app.post("/api/upload/{session_id}")
async def upload_files_endpoint(
    session_id: str, files: List[UploadFile] = File(...)
) -> dict:
    session = _get_or_404(session_id)

    upload_dir = f"/tmp/deploy-sessions/{session_id}"
    Path(upload_dir).mkdir(parents=True, exist_ok=True)
    session.upload_dir = upload_dir

    saved = []
    for f in files:
        # NOTE: Path stripping bug fixed in Task 4.
        safe_path = Path(upload_dir) / Path(f.filename).name
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        content = await f.read()
        safe_path.write_bytes(content)
        saved.append(f.filename)

    session.files = session.files + saved
    store.save(session)
    return {"uploaded": saved, "total_files": len(session.files)}


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


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


app.mount(
    "/",
    StaticFiles(directory=str(Path(__file__).parent / "static"), html=True),
    name="static",
)
```

Note: this version requires `/api/session` to be called before any `/api/upload` or `/api/chat` (returns 404 otherwise). The original code silently created sessions on demand; we tighten that contract because the SQLite store will too.

- [ ] **Step 2.5: Smoke-check the refactored app**

Run:
```bash
cd deploy-agent
./run.sh
```
In another terminal:
```bash
curl -s http://localhost:8000/api/health
```
Expected: `{"status":"ok"}`.

```bash
SESSION=$(curl -s http://localhost:8000/api/session | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
echo "$SESSION"
```
Expected: a UUID printed.

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$SESSION\",\"message\":\"hello\"}" \
  http://localhost:8000/api/chat | python3 -m json.tool
```
Expected: a JSON response with a `"message"` field containing a greeting from the agent. Ctrl-C the server.

- [ ] **Step 2.6: Commit**

```bash
git add deploy-agent/agent.py deploy-agent/sessions.py deploy-agent/app.py deploy-agent/tools.py
git commit -m "refactor: split app.py into agent + sessions modules"
```

---

## Task 3: SQLite-backed `SessionStore`

**Files:**
- Create: `deploy-agent/tests/__init__.py`
- Create: `deploy-agent/tests/conftest.py`
- Create: `deploy-agent/tests/test_sessions.py`
- Modify: `deploy-agent/sessions.py` (add `SqliteSessionStore`)
- Modify: `deploy-agent/app.py` (swap `InMemorySessionStore` → `SqliteSessionStore`)
- Create: `deploy-agent/data/.gitkeep` (so the dir exists in fresh clones)

- [ ] **Step 3.1: Install dev dependencies (one-time)**

```bash
cd deploy-agent
pip3 install pytest moto ruff
```
Expected: pytest, moto, ruff installed.

- [ ] **Step 3.2: Write the failing test for `SqliteSessionStore`**

Create `deploy-agent/tests/__init__.py` (empty file).

Create `deploy-agent/tests/conftest.py`:
```python
"""Pytest fixtures shared across test modules."""

import sys
from pathlib import Path

# Make `deploy-agent/` importable as the source root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

Create `deploy-agent/tests/test_sessions.py`:
```python
"""Tests for sessions.SqliteSessionStore."""

import json

import pytest

from sessions import Session, SqliteSessionStore


@pytest.fixture
def store(tmp_path):
    return SqliteSessionStore(tmp_path / "sessions.db")


def test_create_returns_session_with_uuid(store):
    s1 = store.create()
    s2 = store.create()
    assert s1.session_id != s2.session_id
    assert len(s1.session_id) == 36  # uuid4


def test_get_unknown_returns_none(store):
    assert store.get("does-not-exist") is None


def test_round_trip_messages_with_mixed_blocks(store):
    s = store.create()
    s.messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hi there"},
                {"type": "tool_use", "id": "tu_1", "name": "deploy", "input": {"x": 1}},
            ],
        },
    ]
    store.save(s)

    loaded = store.get(s.session_id)
    assert loaded is not None
    assert loaded.messages == s.messages


def test_save_persists_files_and_upload_dir(store):
    s = store.create()
    s.files = ["index.html", "style.css"]
    s.upload_dir = "/tmp/foo"
    store.save(s)

    loaded = store.get(s.session_id)
    assert loaded.files == ["index.html", "style.css"]
    assert loaded.upload_dir == "/tmp/foo"


def test_deployment_denormalizes_project_and_env(store, tmp_path):
    s = store.create()
    s.deployment = {
        "bucket_name": "myapp-proto-static",
        "site_url": "https://d123.cloudfront.net",
        "cloudfront_distribution_id": "ABC",
        "project_name": "myapp",
        "env": "proto",
    }
    store.save(s)

    # Inspect the raw row to confirm denormalized columns are populated.
    import sqlite3
    db_path = tmp_path / "sessions.db"
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT project_name, env FROM sessions WHERE session_id = ?",
        (s.session_id,),
    ).fetchone()
    conn.close()
    assert row == ("myapp", "proto")


def test_save_is_idempotent(store):
    s = store.create()
    s.messages = [{"role": "user", "content": "first"}]
    store.save(s)
    s.messages.append({"role": "user", "content": "second"})
    store.save(s)

    loaded = store.get(s.session_id)
    assert len(loaded.messages) == 2
```

- [ ] **Step 3.3: Run tests, confirm they fail with import error**

Run:
```bash
cd deploy-agent
pytest tests/test_sessions.py -v
```
Expected: `ImportError: cannot import name 'SqliteSessionStore'`. That's the failing-test signal.

- [ ] **Step 3.4: Add `SqliteSessionStore` to `sessions.py`**

Append to `deploy-agent/sessions.py`:
```python
import json
import sqlite3
from pathlib import Path


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


class SqliteSessionStore:
    """SessionStore backed by a single SQLite file. Survives process restart."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create(self) -> Session:
        session = Session(session_id=str(uuid4()))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id) VALUES (?)",
                (session.session_id,),
            )
            conn.commit()
        return session

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

    def save(self, session: Session) -> None:
        deployment_json = json.dumps(session.deployment) if session.deployment else None
        project_name = session.deployment.get("project_name") if session.deployment else None
        env = session.deployment.get("env") if session.deployment else None
        with self._connect() as conn:
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
            conn.commit()
```

- [ ] **Step 3.5: Run tests, confirm they pass**

Run:
```bash
cd deploy-agent
pytest tests/test_sessions.py -v
```
Expected: all 6 tests pass.

- [ ] **Step 3.6: Update `app.py` to use `SqliteSessionStore`**

In `deploy-agent/app.py`, replace:
```python
from sessions import InMemorySessionStore, Session
...
store = InMemorySessionStore()
```
with:
```python
import os
from sessions import Session, SqliteSessionStore
...
_DB_PATH = Path(os.environ.get("DEPLOY_AGENT_DB", Path(__file__).parent / "data" / "sessions.db"))
store = SqliteSessionStore(_DB_PATH)
```

The env-var override (`DEPLOY_AGENT_DB`) lets future tests point at a temp DB.

- [ ] **Step 3.7: Add `data/.gitkeep`**

```bash
mkdir -p deploy-agent/data
touch deploy-agent/data/.gitkeep
```

- [ ] **Step 3.8: Sanity test — sessions survive restart**

```bash
cd deploy-agent
./run.sh
```
In another terminal:
```bash
SESSION=$(curl -s http://localhost:8000/api/session | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
curl -s -X POST -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$SESSION\",\"message\":\"remember the number 42\"}" \
  http://localhost:8000/api/chat
```
Ctrl-C the server. Restart `./run.sh`. Then:
```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$SESSION\",\"message\":\"what number did I tell you?\"}" \
  http://localhost:8000/api/chat
```
Expected: the agent's reply mentions 42 (because session history persisted across restart).

- [ ] **Step 3.9: Commit**

```bash
git add deploy-agent/sessions.py deploy-agent/app.py deploy-agent/tests/ deploy-agent/data/.gitkeep
git commit -m "feat: SQLite-backed session store, sessions persist across restart"
```

---

## Task 4: Upload path bug fix

The current `app.py` upload handler strips directory components from filenames (`Path(f.filename).name`), which silently breaks any site with nested assets. Fix it and harden against path traversal.

**Files:**
- Create: `deploy-agent/tests/test_app.py`
- Modify: `deploy-agent/app.py` (upload handler only)

- [ ] **Step 4.1: Write failing tests**

Create `deploy-agent/tests/test_app.py`:
```python
"""Tests for FastAPI route behaviors — focus on the upload-path fix."""

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_AGENT_DB", str(tmp_path / "sessions.db"))
    # Re-import app fresh so the env var is picked up.
    import importlib

    import app as app_module

    importlib.reload(app_module)
    return TestClient(app_module.app)


def _make_session(client) -> str:
    return client.get("/api/session").json()["session_id"]


def test_upload_preserves_nested_path(client):
    session = _make_session(client)
    files = [
        ("files", ("assets/css/main.css", io.BytesIO(b"body{}"), "text/css")),
    ]
    r = client.post(f"/api/upload/{session}", files=files)
    assert r.status_code == 200

    # The file should have landed at the nested location, not flattened.
    upload_dir = Path(f"/tmp/deploy-sessions/{session}")
    assert (upload_dir / "assets/css/main.css").exists()
    assert not (upload_dir / "main.css").exists()


def test_upload_rejects_parent_traversal(client):
    session = _make_session(client)
    files = [
        ("files", ("../etc/passwd", io.BytesIO(b"x"), "text/plain")),
    ]
    r = client.post(f"/api/upload/{session}", files=files)
    assert r.status_code == 400
    assert "Invalid filename" in r.json()["detail"]


def test_upload_rejects_absolute_path(client):
    session = _make_session(client)
    files = [
        ("files", ("/etc/passwd", io.BytesIO(b"x"), "text/plain")),
    ]
    r = client.post(f"/api/upload/{session}", files=files)
    assert r.status_code == 400


def test_upload_flat_filename_still_works(client):
    session = _make_session(client)
    files = [
        ("files", ("index.html", io.BytesIO(b"<html></html>"), "text/html")),
    ]
    r = client.post(f"/api/upload/{session}", files=files)
    assert r.status_code == 200
    upload_dir = Path(f"/tmp/deploy-sessions/{session}")
    assert (upload_dir / "index.html").exists()
```

- [ ] **Step 4.2: Run tests, confirm they fail**

```bash
cd deploy-agent
pytest tests/test_app.py -v
```
Expected: `test_upload_preserves_nested_path` fails (file landed at `main.css` not `assets/css/main.css`); `test_upload_rejects_parent_traversal` fails (got 200, expected 400); `test_upload_rejects_absolute_path` fails similarly. Flat-filename test passes.

- [ ] **Step 4.3: Fix `app.py` upload handler**

In `deploy-agent/app.py`, locate the loop:
```python
saved = []
for f in files:
    # NOTE: Path stripping bug fixed in Task 4.
    safe_path = Path(upload_dir) / Path(f.filename).name
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    content = await f.read()
    safe_path.write_bytes(content)
    saved.append(f.filename)
```

Replace with:
```python
saved = []
for f in files:
    filename = f.filename or ""
    parts = Path(filename).parts
    if not parts or any(p in ("..", "") or p.startswith("/") for p in parts):
        raise HTTPException(400, f"Invalid filename: {filename!r}")
    safe_path = Path(upload_dir) / filename
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    content = await f.read()
    safe_path.write_bytes(content)
    saved.append(filename)
```

- [ ] **Step 4.4: Run tests, confirm they pass**

```bash
cd deploy-agent
pytest tests/test_app.py -v
```
Expected: all 4 tests pass.

- [ ] **Step 4.5: Commit**

```bash
git add deploy-agent/app.py deploy-agent/tests/test_app.py
git commit -m "fix: preserve nested upload paths and reject traversal"
```

---

## Task 5: Friendlier tool errors (`_classify_error`)

**Files:**
- Create: `deploy-agent/tests/test_tools.py`
- Modify: `deploy-agent/tools.py` (add `_classify_error`, use it in `deploy_infrastructure`)
- Modify: `deploy-agent/agent.py` (extend system prompt)

- [ ] **Step 5.1: Write failing tests for `_classify_error`**

Create `deploy-agent/tests/test_tools.py`:
```python
"""Tests for tools._classify_error and deploy_infrastructure error paths."""

from unittest.mock import MagicMock, patch

import pytest

import tools


def test_classify_access_denied():
    result = tools._classify_error("Error: AccessDenied: User: arn:... is not authorized")
    assert "permission" in result["summary"].lower() or "credentials" in result["summary"].lower()
    assert "AccessDenied" in result["details"]


def test_classify_no_credentials():
    result = tools._classify_error("NoCredentialProviders: no valid providers in chain")
    assert "credentials" in result["summary"].lower()


def test_classify_bucket_collision():
    result = tools._classify_error("BucketAlreadyOwnedByYou: bucket already exists")
    assert "project_name" in result["summary"]


def test_classify_unknown_falls_through_to_generic():
    result = tools._classify_error("kaboom: weird internal error")
    assert result["summary"] == "Deployment failed — see details."
    assert "kaboom" in result["details"]


def test_classify_truncates_long_details():
    huge = "x" * 10_000
    result = tools._classify_error(huge)
    assert len(result["details"]) <= 2000


@patch("tools.subprocess.run")
def test_deploy_infrastructure_init_failure_returns_summary(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stderr="NoCredentialProviders: ...")
    result = tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
    )
    assert "summary" in result
    assert "details" in result
    assert "credentials" in result["summary"].lower()


@patch("tools.subprocess.run")
def test_deploy_infrastructure_apply_failure_returns_summary(mock_run):
    # init succeeds, workspace cmds succeed, apply fails
    def fake_run(cmd, **kwargs):
        if cmd[1] == "apply":
            return MagicMock(returncode=1, stderr="AccessDenied: not authorized to s3:CreateBucket")
        return MagicMock(returncode=0, stderr="", stdout="{}")
    mock_run.side_effect = fake_run

    result = tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
    )
    assert "summary" in result
    assert "permission" in result["summary"].lower() or "credentials" in result["summary"].lower()


@patch("tools.subprocess.run")
def test_deploy_infrastructure_happy_path_returns_outputs(mock_run):
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
    )
    assert "summary" not in result
    assert result["bucket_name"] == "x-proto-static"
    assert result["site_url"] == "https://d.cloudfront.net"
    assert result["project_name"] == "x"
    assert result["env"] == "proto"


# ── upload_files tests (with moto) ────────────────────────────────────────────


@pytest.fixture
def aws_credentials(monkeypatch):
    """Stub AWS env vars so moto/boto3 is happy without real creds."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def test_upload_files_uploads_nested_with_correct_key(aws_credentials, tmp_path):
    from moto import mock_aws
    import boto3

    from sessions import Session

    upload_dir = tmp_path / "upload"
    (upload_dir / "assets" / "css").mkdir(parents=True)
    (upload_dir / "index.html").write_text("<html></html>")
    (upload_dir / "assets" / "css" / "main.css").write_text("body{}")

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        cf = boto3.client("cloudfront", region_name="us-east-1")
        # moto's CloudFront mock requires a real-ish distribution; skip create — invalidation
        # call is what we want to verify, and moto raises NoSuchDistribution if missing,
        # so we patch the CF call instead.
        with patch.object(tools.boto3, "client") as mock_client:
            real_s3 = s3
            mock_cf = MagicMock()
            mock_client.side_effect = lambda name, *a, **kw: real_s3 if name == "s3" else mock_cf

            session = Session(session_id="s", upload_dir=str(upload_dir))
            result = tools.upload_files(
                bucket_name="test-bucket",
                distribution_id="DIST123",
                session=session,
            )

            assert result["uploaded_count"] == 2
            assert "assets/css/main.css" in result["files"]
            assert "index.html" in result["files"]

            keys = {o["Key"] for o in real_s3.list_objects_v2(Bucket="test-bucket")["Contents"]}
            assert keys == {"index.html", "assets/css/main.css"}

            mock_cf.create_invalidation.assert_called_once()
            inv_args = mock_cf.create_invalidation.call_args.kwargs
            assert inv_args["DistributionId"] == "DIST123"
            assert inv_args["InvalidationBatch"]["Paths"]["Items"] == ["/*"]


def test_upload_files_returns_summary_when_dir_missing(tmp_path):
    from sessions import Session
    session = Session(session_id="s", upload_dir=str(tmp_path / "does-not-exist"))
    result = tools.upload_files(
        bucket_name="b", distribution_id="d", session=session,
    )
    assert "summary" in result
    assert "No uploaded files" in result["summary"]
```

- [ ] **Step 5.2: Run tests, confirm they fail**

```bash
cd deploy-agent
pytest tests/test_tools.py -v
```
Expected: all tests fail (`AttributeError: module 'tools' has no attribute '_classify_error'` and similar).

- [ ] **Step 5.3: Add `_classify_error` and update `deploy_infrastructure`**

In `deploy-agent/tools.py`:

Add near the top (after the existing imports):
```python
import re

_ERROR_PATTERNS: list[tuple[str, str]] = [
    (r"NoCredentialProviders|Unable to locate credentials",
     "No AWS credentials found. Set AWS_PROFILE or AWS_ACCESS_KEY_ID."),
    (r"AccessDenied|UnauthorizedOperation|is not authorized to",
     "AWS credentials lack permission for this operation. Check IAM."),
    (r"BucketAlreadyOwnedByYou|BucketAlreadyExists",
     "A bucket with this name already exists in your account. Pick a different project_name."),
    (r"Error: error configuring",
     "AWS configuration error — check your region and credentials."),
]


def _classify_error(stderr: str) -> dict:
    """Map raw stderr to a {summary, details} dict for the agent to surface."""
    details = stderr[-2000:]
    for pattern, summary in _ERROR_PATTERNS:
        if re.search(pattern, stderr):
            return {"summary": summary, "details": details}
    return {"summary": "Deployment failed — see details.", "details": details}
```

Replace the `deploy_infrastructure` function body's error returns. Find:
```python
        if r.returncode != 0:
            return {"error": f"tofu init failed:\n{r.stderr[-2000:]}"}
```
Replace with:
```python
        if r.returncode != 0:
            return _classify_error(r.stderr)
```

Find:
```python
        if r.returncode != 0:
            return {"error": f"tofu apply failed:\n{r.stderr[-3000:]}"}
```
Replace with:
```python
        if r.returncode != 0:
            return _classify_error(r.stderr)
```

Find:
```python
    except subprocess.TimeoutExpired:
        return {"error": "Deployment timed out after 10 minutes."}
    except Exception as e:
        return {"error": str(e)}
```
Replace with:
```python
    except subprocess.TimeoutExpired:
        return {"summary": "Deployment timed out after 10 minutes.", "details": ""}
    except Exception as e:
        return {"summary": "Deployment failed unexpectedly.", "details": str(e)}
```

Also update the success return to include `project_name` and `env` (used by `destroy_all.py` later):

Find:
```python
        return {
            "bucket_name":              outputs["bucket_name"]["value"],
            "site_url":                 outputs["site_url"]["value"],
            "cloudfront_distribution_id": outputs["cloudfront_distribution_id"]["value"],
        }
```
Replace with:
```python
        return {
            "bucket_name":              outputs["bucket_name"]["value"],
            "site_url":                 outputs["site_url"]["value"],
            "cloudfront_distribution_id": outputs["cloudfront_distribution_id"]["value"],
            "project_name":             project_name,
            "env":                      env,
        }
```

- [ ] **Step 5.4: Update `upload_files` to use the same shape on errors**

In `tools.py`, find:
```python
    except Exception as e:
        return {"error": str(e)}
```
inside `upload_files`. Replace with:
```python
    except Exception as e:
        return {"summary": "File upload failed.", "details": str(e)}
```

Also replace the early-return:
```python
    if not upload_dir or not Path(upload_dir).exists():
        return {"error": "No uploaded files found for this session."}
```
with:
```python
    if not upload_dir or not Path(upload_dir).exists():
        return {"summary": "No uploaded files found for this session.", "details": ""}
```

- [ ] **Step 5.5: Update system prompt in `agent.py`**

In `deploy-agent/agent.py`, find the `Rules:` section in `SYSTEM_PROMPT` and replace this line:
```
- If a tool returns an error, explain it simply and suggest what to check.
```
with:
```
- If a tool result contains a `summary` field, that means the call failed. Tell the user the summary in plain language and offer to share the `details` if they ask. Suggest what to check based on the summary.
```

- [ ] **Step 5.6: Run tests, confirm they pass**

```bash
cd deploy-agent
pytest tests/test_tools.py tests/test_sessions.py tests/test_app.py -v
```
Expected: all tests pass (sessions and app tests should still pass too).

- [ ] **Step 5.7: Commit**

```bash
git add deploy-agent/tools.py deploy-agent/agent.py deploy-agent/tests/test_tools.py
git commit -m "feat: structured tool errors with friendly summaries"
```

---

## Task 6: Extract `MAX_AGENT_ITERATIONS`

This is the smallest task. The constant already exists in `agent.py` (added in Task 2). We add a test that proves it's respected and overridable.

**Files:**
- Create: `deploy-agent/tests/test_agent.py`

- [ ] **Step 6.1: Write the test**

Create `deploy-agent/tests/test_agent.py`:
```python
"""Tests for agent.run_agent_loop."""

from unittest.mock import MagicMock

import pytest

import agent
from sessions import Session


def _block(type_, **kwargs):
    """Build a fake Anthropic ContentBlock that supports model_dump and attribute access."""
    m = MagicMock()
    m.type = type_
    m.model_dump.return_value = {"type": type_, **kwargs}
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _response(content, stop_reason):
    r = MagicMock()
    r.content = content
    r.stop_reason = stop_reason
    return r


def test_happy_path_returns_text(monkeypatch):
    client = MagicMock()
    client.messages.create.return_value = _response(
        [_block("text", text="all done")], "end_turn"
    )
    session = Session(session_id="s1")
    session.messages.append({"role": "user", "content": "hi"})

    result = agent.run_agent_loop(client, session)
    assert result == "all done"
    # Assistant turn was appended with serialized content (dicts, not MagicMocks).
    last = session.messages[-1]
    assert last["role"] == "assistant"
    assert last["content"] == [{"type": "text", "text": "all done"}]


def test_iteration_cap_hits_safety_limit(monkeypatch):
    monkeypatch.setattr(agent, "MAX_AGENT_ITERATIONS", 2)

    client = MagicMock()
    # Always returns a tool_use block for an unknown tool — never end_turn.
    client.messages.create.return_value = _response(
        [_block("tool_use", id="t1", name="nonexistent", input={})], "tool_use"
    )

    # Patch execute_tool to return a benign no-op so the loop continues.
    monkeypatch.setattr(agent, "execute_tool",
                        lambda name, inputs, session_id, session: {"summary": "noop", "details": ""})

    session = Session(session_id="s1")
    result = agent.run_agent_loop(client, session)
    assert "iteration limit" in result
    assert client.messages.create.call_count == 2  # respected MAX_AGENT_ITERATIONS


def test_tool_use_round_trip_caches_deployment(monkeypatch):
    client = MagicMock()
    deploy_block = _block("tool_use", id="t1", name="deploy_infrastructure", input={})
    client.messages.create.side_effect = [
        _response([deploy_block], "tool_use"),
        _response([_block("text", text="✅ Live!")], "end_turn"),
    ]
    monkeypatch.setattr(
        agent, "execute_tool",
        lambda name, inputs, session_id, session: {
            "bucket_name": "b", "site_url": "https://x", "cloudfront_distribution_id": "C",
            "project_name": "p", "env": "proto",
        },
    )

    session = Session(session_id="s1")
    result = agent.run_agent_loop(client, session)
    assert result == "✅ Live!"
    assert session.deployment is not None
    assert session.deployment["bucket_name"] == "b"


def test_tool_failure_does_not_cache_deployment(monkeypatch):
    client = MagicMock()
    deploy_block = _block("tool_use", id="t1", name="deploy_infrastructure", input={})
    client.messages.create.side_effect = [
        _response([deploy_block], "tool_use"),
        _response([_block("text", text="that failed")], "end_turn"),
    ]
    monkeypatch.setattr(
        agent, "execute_tool",
        lambda name, inputs, session_id, session: {"summary": "boom", "details": "..."},
    )

    session = Session(session_id="s1")
    agent.run_agent_loop(client, session)
    assert session.deployment is None
```

- [ ] **Step 6.2: Run tests, confirm they pass**

```bash
cd deploy-agent
pytest tests/test_agent.py -v
```
Expected: all 4 tests pass (no implementation changes needed — `agent.py` is already correct from Task 2 and Task 5).

- [ ] **Step 6.3: Commit**

```bash
git add deploy-agent/tests/test_agent.py
git commit -m "test: cover agent loop happy/failure paths and iteration cap"
```

---

## Task 7: Sample site, smoke test, destroy_all

**Files:**
- Create: `examples/sample-site/index.html`
- Create: `examples/sample-site/style.css`
- Create: `examples/sample-site/assets/logo.svg`
- Create: `scripts/smoke-test.sh`
- Create: `scripts/destroy_all.py`

- [ ] **Step 7.1: Create the sample site**

Create `examples/sample-site/index.html`:
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>INFRA Deploy Agent — Sample Site</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main>
    <img src="assets/logo.svg" alt="logo" width="80" height="80">
    <h1>Hello from the cloud!</h1>
    <p data-marker="deployed-via-infra-deploy-agent">
      This page was deployed via INFRA Deploy Agent.
    </p>
  </main>
</body>
</html>
```

The `data-marker` attribute is a stable string the smoke test greps for to confirm a successful deploy.

Create `examples/sample-site/style.css`:
```css
:root { color-scheme: light dark; }
body {
  font: 16px/1.6 system-ui, sans-serif;
  display: grid;
  place-items: center;
  min-height: 100vh;
  margin: 0;
}
main { text-align: center; padding: 2rem; }
h1 { margin: 1rem 0 .5rem; }
```

Create `examples/sample-site/assets/logo.svg`:
```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <circle cx="32" cy="32" r="28" fill="#0a84ff"/>
  <text x="32" y="40" text-anchor="middle" font-family="system-ui" font-size="24" fill="white">⬡</text>
</svg>
```

The nested `assets/` path is intentional — uploading this site exercises the path-preservation fix from Task 4.

- [ ] **Step 7.2: Create `scripts/destroy_all.py`**

Create the file:
```python
#!/usr/bin/env python3
"""Tear down every deployment recorded in the SQLite session store.

Reads project_name/env from each session's deployment row, runs `tofu workspace
select` + `tofu destroy` for each. Skips and logs sessions whose workspace no
longer exists (e.g., already destroyed manually).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "deploy-agent" / "data" / "sessions.db"
STACK_DIR = REPO_ROOT / "infra" / "stacks" / "static-website"


def main() -> int:
    db_path = Path(os.environ.get("DEPLOY_AGENT_DB", DEFAULT_DB))
    if not db_path.exists():
        print(f"No session DB at {db_path}; nothing to destroy.")
        return 0

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT session_id, project_name, env FROM sessions "
        "WHERE deployment IS NOT NULL AND project_name IS NOT NULL"
    ).fetchall()
    conn.close()

    if not rows:
        print("No deployments recorded.")
        return 0

    print(f"Found {len(rows)} deployment(s) to destroy.")
    failures = 0
    for session_id, project, env in rows:
        workspace = f"{project}-{env}"
        print(f"\n→ Destroying {workspace} (session {session_id[:8]}…)")

        select = subprocess.run(
            ["tofu", "workspace", "select", workspace],
            cwd=STACK_DIR, capture_output=True, text=True,
        )
        if select.returncode != 0:
            print(f"  workspace select failed (likely already destroyed): "
                  f"{select.stderr.strip()[:200]}")
            continue

        destroy = subprocess.run(
            [
                "tofu", "destroy", "-auto-approve", "-input=false",
                f"-var=project_name={project}",
                f"-var=env={env}",
            ],
            cwd=STACK_DIR,
        )
        if destroy.returncode != 0:
            print(f"  destroy failed (rc={destroy.returncode})")
            failures += 1
        else:
            print("  destroyed.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
```

Make it executable:
```bash
chmod +x scripts/destroy_all.py
```

- [ ] **Step 7.3: Create `scripts/smoke-test.sh`**

```bash
#!/usr/bin/env bash
# Full deploy → upload → verify → destroy cycle against a real AWS account.
# Requires: ANTHROPIC_API_KEY, AWS credentials (AWS_PROFILE or AWS_ACCESS_KEY_ID).
# Cost: ~$0.01 per run. Cleanup runs even on failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="smoke-$(date +%s)"
ENV="smoke"
PORT=8765
SERVER_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  echo "→ Cleanup: destroying smoke deployment ($PROJECT-$ENV)..."
  ( cd "$REPO_ROOT/infra/stacks/static-website" \
      && tofu workspace select "$PROJECT-$ENV" 2>/dev/null \
      && tofu destroy -auto-approve -input=false \
           -var="project_name=$PROJECT" -var="env=$ENV" ) || true
}
trap cleanup EXIT

require() { command -v "$1" >/dev/null || { echo "missing: $1"; exit 2; }; }
require curl
require tofu
require python3

[ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "ANTHROPIC_API_KEY required"; exit 2; }

echo "→ Starting agent server on :$PORT..."
cd "$REPO_ROOT/deploy-agent"
DEPLOY_AGENT_DB="$(mktemp -d)/smoke-sessions.db" \
  uvicorn app:app --host 127.0.0.1 --port "$PORT" --log-level warning &
SERVER_PID=$!

# Wait for server.
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null && break
  sleep 1
done
curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null || { echo "server did not start"; exit 3; }

echo "→ Creating session..."
SESSION=$(curl -sf "http://127.0.0.1:$PORT/api/session" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")

echo "→ Uploading sample-site..."
cd "$REPO_ROOT/examples/sample-site"
curl -sf \
  -F "files=@index.html;filename=index.html" \
  -F "files=@style.css;filename=style.css" \
  -F "files=@assets/logo.svg;filename=assets/logo.svg" \
  "http://127.0.0.1:$PORT/api/upload/$SESSION" >/dev/null

echo "→ Asking agent to deploy (project=$PROJECT)..."
MSG="Deploy this site. site_title=Smoke Test, owner_name=Smoke Bot, owner_email=smoke@example.com, is_spa=false. Use project_name=$PROJECT and env=$ENV."
RESPONSE=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json,sys;print(json.dumps({'session_id':sys.argv[1],'message':sys.argv[2]}))" "$SESSION" "$MSG")" \
  "http://127.0.0.1:$PORT/api/chat")

SITE_URL=$(echo "$RESPONSE" | python3 -c "import sys,json;d=json.load(sys.stdin).get('deployment') or {};print(d.get('site_url') or '')")
[ -n "$SITE_URL" ] || { echo "agent did not deploy. Response: $RESPONSE"; exit 4; }
echo "→ Site URL: $SITE_URL"

echo "→ Waiting for CloudFront propagation (up to 5 min)..."
for _ in $(seq 1 60); do
  BODY=$(curl -sf -L "$SITE_URL" || true)
  if echo "$BODY" | grep -q "deployed-via-infra-deploy-agent"; then
    echo "✓ Smoke test passed."
    exit 0
  fi
  sleep 5
done

echo "✗ Marker string not found in deployed site after 5 min."
exit 5
```

Make it executable:
```bash
chmod +x scripts/smoke-test.sh
```

- [ ] **Step 7.4: Run the smoke test (one-time real-AWS verification)**

```bash
./scripts/smoke-test.sh
```
Expected: `✓ Smoke test passed.` after ~3-7 minutes (deploy + propagation + destroy). Cost: ~$0.01.

If it fails, the trap should still destroy the resources. Verify with:
```bash
cd infra/stacks/static-website && tofu workspace list
```
There should be no `smoke-*` workspaces remaining.

- [ ] **Step 7.5: Commit**

```bash
git add examples/ scripts/
git commit -m "feat: sample site, smoke test, and destroy_all helper"
```

---

## Task 8: Pin dependencies, add `pyproject.toml`, add Makefile

**Files:**
- Modify: `deploy-agent/requirements.txt` (pin versions)
- Create: `deploy-agent/pyproject.toml`
- Create: `deploy-agent/Makefile`

- [ ] **Step 8.1: Pin `requirements.txt`**

Run from `deploy-agent/`:
```bash
pip3 freeze | grep -iE '^(fastapi|uvicorn|anthropic|boto3|python-multipart|starlette|pydantic|httpx|h11|click|annotated-types|sniffio|anyio|certifi|charset-normalizer|distro|idna|jiter|requests|s3transfer|botocore|jmespath|python-dateutil|six|typing-extensions|urllib3|tokenizers|huggingface-hub|pyyaml|filelock|fsspec|tqdm)=='
```

Take the output and write it as the new `deploy-agent/requirements.txt`. If the freeze output is missing direct deps, add at minimum these pinned versions (use whichever versions `pip freeze` showed, or the latest stable at time of writing — write exact `==` pins, not `>=`):

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
anthropic==0.40.0
boto3==1.35.97
python-multipart==0.0.20
```

Add a separate dev section by creating `deploy-agent/requirements-dev.txt`:
```
pytest==8.3.4
moto[s3,cloudfront]==5.0.28
ruff==0.9.2
```

- [ ] **Step 8.2: Create `deploy-agent/pyproject.toml`**

```toml
[project]
name = "deploy-agent"
version = "0.1.0"
description = "Chat-driven AWS static-site deployer"
requires-python = ">=3.11"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "RUF"]
ignore = ["E501"]  # let formatter handle line length

[tool.ruff.format]
quote-style = "double"

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
```

- [ ] **Step 8.3: Create `deploy-agent/Makefile`**

```make
.PHONY: check lint format test infra-validate destroy-all install install-dev

install:
	pip3 install -r requirements.txt

install-dev: install
	pip3 install -r requirements-dev.txt

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

test:
	pytest tests/ -v

infra-validate:
	cd ../infra/stacks/static-website && \
	  tofu fmt -recursive -check && \
	  tofu init -backend=false -upgrade && \
	  tofu validate

check: lint test infra-validate

destroy-all:
	python3 ../scripts/destroy_all.py
```

- [ ] **Step 8.4: Run `make check`**

```bash
cd deploy-agent
make install-dev
make check
```
Expected: ruff passes (may need a `make format` first to fix imports), pytest all green, `tofu validate` reports `Success!`.

If ruff complains about anything, run `make format` and inspect the diff before committing.

- [ ] **Step 8.5: Commit**

```bash
git add deploy-agent/requirements.txt deploy-agent/requirements-dev.txt \
        deploy-agent/pyproject.toml deploy-agent/Makefile
git commit -m "chore: pin dependencies, add ruff config, add make check"
```

---

## Task 9: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 9.1: Create the workflow**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install Python deps
        working-directory: deploy-agent
        run: |
          pip install -r requirements.txt
          pip install -r requirements-dev.txt

      - name: Lint
        working-directory: deploy-agent
        run: |
          ruff check .
          ruff format --check .

      - name: Test
        working-directory: deploy-agent
        run: pytest tests/ -v

      - uses: opentofu/setup-opentofu@v1
        with:
          tofu_version: "1.8.0"

      - name: Tofu fmt check
        working-directory: infra
        run: tofu fmt -recursive -check

      - name: Tofu validate
        working-directory: infra/stacks/static-website
        run: |
          tofu init -backend=false
          tofu validate
```

- [ ] **Step 9.2: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: lint, test, and tofu validate on push/PR"
```

CI will only run after the repo is on GitHub (Task 11). We commit it now so it's live the moment the repo is pushed.

---

## Task 10: README final pass + CLAUDE.md update

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 10.1: Replace `README.md` with the full version**

Overwrite `README.md` with:

```markdown
# INFRA Deploy Agent

Chat with an AI agent to deploy a static website to AWS (S3 + CloudFront). Drag a folder of HTML/CSS/JS into the chat, answer a few questions, and the agent provisions the infrastructure with OpenTofu and uploads your files. Friend-of-the-engineer level prototype.

## Quickstart

```bash
git clone https://github.com/<you>/infra-prototypes.git
cd infra-prototypes/deploy-agent
make install
export ANTHROPIC_API_KEY=sk-ant-...
export AWS_PROFILE=...        # or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
./run.sh
# open http://localhost:8000, drag examples/sample-site/ into the chat
```

The agent will collect a site title, owner name, and email, then call `deploy_infrastructure` (provisions S3 + CloudFront) and `upload_files` (syncs your folder + invalidates the CDN cache). It returns the live URL.

## What it costs

CloudFront has no idle cost beyond the distribution fee (~$0.50/month). Egress is the bigger lever — for a low-traffic prototype, expect under $1/month total. To clean up:

```bash
cd infra && make destroy PROJECT=<your-project> ENV=<your-env>
# or to wipe every deployment recorded in the local session DB:
cd deploy-agent && make destroy-all
```

## Architecture

```
┌──────────────┐    /api/chat      ┌──────────────────┐
│  Static UI   │ ─────────────────▶│  FastAPI agent   │
│ static/      │                   │  app.py          │
└──────────────┘                   │  agent.py        │
                                   │  sessions.py     │
                                   │  tools.py        │
                                   └────────┬─────────┘
                                            │ tofu / boto3
                                   ┌────────▼─────────┐
                                   │  infra/          │
                                   │  modules + stack │
                                   └────────┬─────────┘
                                            │
                                   ┌────────▼─────────┐
                                   │  AWS: S3 + CF    │
                                   └──────────────────┘
```

The agent loop in `agent.py` runs Claude with two tools (`deploy_infrastructure`, `upload_files`), executes them via subprocess (`tofu`) and `boto3`, and feeds the structured results back to Claude until it produces a final text response. Sessions persist in `deploy-agent/data/sessions.db`.

## Verifying it works

`./scripts/smoke-test.sh` runs a full deploy → upload → assert-content → destroy cycle against your AWS account. ~$0.01 per run. Cleanup runs on failure.

## Project layout

```
deploy-agent/
  app.py            FastAPI route handlers
  agent.py          Claude tool-use loop + system prompt
  sessions.py       SQLite-backed session store
  tools.py          deploy_infrastructure, upload_files
  static/           Chat UI
  tests/            pytest suite (mocked, no AWS calls)
infra/
  modules/          Reusable s3-static-site, cloudfront modules
  stacks/           static-website stack composition
examples/
  sample-site/      Drag-in example, also fixture for smoke test
scripts/
  smoke-test.sh     Real-AWS smoke test
  destroy_all.py    Tears down every deployment in the session DB
docs/
  superpowers/      Design docs and implementation plans
```

## Limitations

- **Local OpenTofu state.** Fine for one developer. Multi-user requires migrating to S3+DynamoDB backend (`infra/stacks/static-website/backend.tf` has the migration block ready).
- **One stack type.** Hardcoded to `static-website`. Future stacks (Lambda, ECS) require a `stack_name` parameter on `deploy_infrastructure`.
- **No auth on the FastAPI server.** Bind to `127.0.0.1` if running on a shared machine.
- **Single-tenant.** Concurrent requests against the same session aren't safe; design assumes one user, one tab.

## Contributing

```bash
cd deploy-agent
make install-dev
make check     # ruff + pytest + tofu validate
```

CI runs the same on every push and PR.

## License

MIT — add a `LICENSE` file before publishing.
```

- [ ] **Step 10.2: Update `CLAUDE.md` to reflect the new module split**

Modify the "Architecture" section in `CLAUDE.md`. Find:

```
### Agent loop (`deploy-agent/app.py`)
`run_agent_loop` is a hand-rolled tool-use loop with a 15-iteration safety cap (`app.py:79`). Each turn:
```

Replace with:
```
### Agent loop (`deploy-agent/agent.py`)
`run_agent_loop` is a hand-rolled tool-use loop with `MAX_AGENT_ITERATIONS = 15` (overridable in tests). Each turn:
```

Find:
```
Sessions live in an in-memory `dict` keyed by UUID (`app.py:25`). Restarting the server drops all history — fine for a prototype, swap for Redis if needed (note in code).
```
Replace with:
```
Sessions are persisted in a SQLite file at `deploy-agent/data/sessions.db` via `SqliteSessionStore` in `sessions.py`. Schema is single-table; `project_name` and `env` are denormalized columns extracted from `deployment` so `scripts/destroy_all.py` can `SELECT` them without parsing JSON. Override the path with `DEPLOY_AGENT_DB` env var (used by tests).
```

Find the "Things that will bite you" section's bullet about `Path(f.filename).name`:
```
- File uploads in `/api/upload/{session_id}` use `Path(f.filename).name` (`app.py:152`), which **strips relative paths** like `dist/index.html` down to `index.html`. If you need to preserve directory structure for nested assets, that's where to fix it.
```
Replace with:
```
- File uploads in `/api/upload/{session_id}` preserve relative paths and reject `..` traversal / absolute paths with HTTP 400. `examples/sample-site/assets/logo.svg` is the regression fixture — if you break path preservation, the smoke test will fail.
```

- [ ] **Step 10.3: Run `make check` once more**

```bash
cd deploy-agent
make check
```
Expected: green.

- [ ] **Step 10.4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: full README + update CLAUDE.md for new module split"
```

---

## Task 11: Push to GitHub

**Files:** none.

- [ ] **Step 11.1: Confirm `gh` is authenticated**

```bash
gh auth status
```
Expected: shows your GitHub login. If not, run `gh auth login` (the user does this interactively — do not script it).

- [ ] **Step 11.2: Create the public repo**

Decide on a name. Recommended: `infra-prototypes` (matches local folder semantically without the space).

```bash
gh repo create infra-prototypes --public \
  --description "Chat-driven AWS static-site deployer (Claude + OpenTofu)" \
  --source=. --push
```
Expected: repo created at `https://github.com/<you>/infra-prototypes`, default branch `main`, all commits pushed.

- [ ] **Step 11.3: Verify CI runs green**

```bash
gh run watch
```
Expected: the CI workflow runs lint + test + tofu validate, all pass within 60s.

If anything fails, fix it on a branch, push, ensure CI is green, then merge. Do not force-push to `main`.

- [ ] **Step 11.4: Final sanity — README renders correctly**

```bash
gh repo view --web
```
Skim the rendered README; confirm code blocks render, no broken markdown.

- [ ] **Step 11.5: Update plan + spec status**

Edit the spec at `docs/superpowers/specs/2026-05-05-deploy-agent-hardening-design.md` to add a `## Status` section at the top:
```markdown
## Status

✅ Implemented and shipped on YYYY-MM-DD as commits on `main`. Repo at https://github.com/<you>/infra-prototypes.
```

Commit:
```bash
git add docs/superpowers/specs/2026-05-05-deploy-agent-hardening-design.md
git commit -m "docs: mark hardening spec as shipped"
git push
```

---

## Done criteria

- `make check` passes locally and in CI.
- A fresh clone + `make install && ANTHROPIC_API_KEY=… AWS_PROFILE=… ./run.sh` works.
- `./scripts/smoke-test.sh` passes against real AWS.
- Restarting the server preserves chat history.
- Uploading a folder with nested assets (`assets/logo.svg`) preserves the path.
- GitHub repo is public, CI badge is green.

When all six are true, sub-project A is done. Sub-project B (host the agent for strangers) is the next brainstorm.
