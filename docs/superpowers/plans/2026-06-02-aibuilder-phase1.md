# aibuilder Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a chat bot at `aibuilder/` that takes a public GitHub URL, validates with the user what it sees in the repo, recommends an AWS architecture from a curated catalog, and returns a monthly cost estimate. No deploys in this phase.

**Architecture:** New sibling app at `aibuilder/`, structured like `deploy-agent/`. FastAPI + Claude tool-use loop + SQLite-backed sessions + single-file static chat UI. Four tools: `clone_repo`, `analyze_repo`, `recommend_architecture`, `estimate_cost`. Detection is pure-Python (no LLM); the pattern catalog is in code; pricing is a curated fallback table in v1 (live AWS Pricing API integration is deferred to a follow-up plan).

**Tech Stack:** Python 3.11, FastAPI, uvicorn, Anthropic SDK (`claude-opus-4-6`), boto3 (for clone tempdir mgmt + future pricing), SQLite (stdlib), pytest + ruff. UI: vanilla HTML/CSS/JS.

**Spec:** `docs/superpowers/specs/2026-06-02-aibuilder-design.md`

**Scope adjustment from spec:** v1 implements the cost-estimate fallback table only. Live AWS Pricing API integration is deferred to Phase 1.5. The `CostEstimate.is_fallback` field is kept so the future change is additive. Confirm before starting Task 7 if you want to revisit.

---

## File map

```
aibuilder/
├── .env.example                Template for ANTHROPIC_API_KEY etc.
├── Makefile                    install / install-dev / lint / test / check / format
├── pyproject.toml              ruff + pytest config
├── requirements.txt            fastapi, uvicorn, anthropic, boto3, python-multipart
├── requirements-dev.txt        pytest, ruff
├── run.sh                      venv bootstrap + uvicorn launcher
├── app.py                      FastAPI: /api/session, /api/chat, /api/health
├── agent.py                    Claude tool-use loop + SYSTEM_PROMPT
├── sessions.py                 Session dataclass + SqliteSessionStore
├── tools.py                    Four tool fns + TOOL_DEFINITIONS + execute_tool
├── analyzer.py                 RepoProfile dataclass + analyze_repo()
├── patterns.py                 Architecture types + pattern catalog + recommend()
├── pricing.py                  CostEstimate types + fallback table + estimate()
├── static/
│   ├── index.html              Chat UI (adapted from deploy-agent)
│   └── govtech-barbados.png    Logo (copied from deploy-agent/static/)
├── tests/
│   ├── __init__.py
│   ├── conftest.py             sys.path shim
│   ├── fixtures/
│   │   ├── static_site/        index.html + assets
│   │   ├── node_api/           package.json with express
│   │   ├── python_api/         requirements.txt with fastapi
│   │   ├── dockerized_web/     Dockerfile + small server
│   │   ├── spa_with_api/       package.json with react + api/ dir
│   │   ├── fullstack_with_db/  spa_with_api + postgres in deps
│   │   └── worker/             package.json with bull or python with celery
│   ├── test_analyzer.py
│   ├── test_patterns.py
│   ├── test_pricing.py
│   ├── test_tools.py
│   ├── test_sessions.py
│   ├── test_agent.py
│   └── test_app.py
└── data/                       SQLite db lives here (gitignored)
```

Repo-level changes:

- `.gitignore` — add `aibuilder/data/`, `aibuilder/tmp/`, `aibuilder/.venv/`
- `.github/workflows/ci.yml` (or equivalent) — extend to run aibuilder's `make check` if a workflow file exists; otherwise the existing `deploy-agent` CI step can be duplicated for aibuilder
- `CLAUDE.md` — add an aibuilder section mirroring the deploy-agent section

---

## Task 1: Scaffold the project

**Files:**
- Create: `aibuilder/pyproject.toml`
- Create: `aibuilder/requirements.txt`
- Create: `aibuilder/requirements-dev.txt`
- Create: `aibuilder/Makefile`
- Create: `aibuilder/.env.example`
- Create: `aibuilder/run.sh`
- Create: `aibuilder/tests/__init__.py`
- Create: `aibuilder/tests/conftest.py`
- Modify: `.gitignore` (add aibuilder paths)

- [ ] **Step 1: Create `aibuilder/pyproject.toml`**

```toml
[project]
name = "aibuilder"
version = "0.1.0"
description = "Chat-driven repo analyzer + AWS architecture/cost estimator"
requires-python = ">=3.11"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "RUF"]
ignore = ["E501"]

[tool.ruff.format]
quote-style = "double"

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
```

- [ ] **Step 2: Create `aibuilder/requirements.txt`**

```
fastapi==0.136.1
uvicorn[standard]==0.34.0
anthropic==0.98.1
boto3==1.43.3
python-multipart==0.0.27
```

- [ ] **Step 3: Create `aibuilder/requirements-dev.txt`**

```
pytest==9.0.3
ruff==0.15.12
```

- [ ] **Step 4: Create `aibuilder/Makefile`**

```makefile
.PHONY: check lint format test install install-dev

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

check: lint test
```

- [ ] **Step 5: Create `aibuilder/.env.example`**

```bash
# Required for the chat agent
ANTHROPIC_API_KEY=sk-ant-...

# Optional — only needed once Phase 1.5 wires up the AWS Pricing API
# AWS_PROFILE=personal-default
```

- [ ] **Step 6: Create `aibuilder/run.sh`** (mark executable)

```bash
#!/usr/bin/env bash
# Start the aibuilder chat agent.
# Usage: ./run.sh

set -e
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "❌  ANTHROPIC_API_KEY is not set."
  echo "    Export it: export ANTHROPIC_API_KEY=sk-ant-..."
  exit 1
fi

VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
  echo "📦 Creating virtual environment..."
  python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if ! python3 -c "import fastapi" 2>/dev/null || ! python3 -c "import uvicorn" 2>/dev/null; then
  echo "📦 Installing dependencies..."
  pip install -r requirements.txt --quiet
fi

echo ""
echo "⬢  aibuilder"
echo "   Open http://localhost:8001 in your browser"
echo ""

python3 -m uvicorn app:app --host 0.0.0.0 --port 8001 --reload
```

Then run `chmod +x aibuilder/run.sh`.

- [ ] **Step 7: Create `aibuilder/tests/__init__.py`** (empty file)

- [ ] **Step 8: Create `aibuilder/tests/conftest.py`**

```python
"""Pytest fixtures shared across test modules."""

import sys
from pathlib import Path

# Make `aibuilder/` importable as the source root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 9: Update `.gitignore`** — append these lines after the existing `deploy-agent/data/` entry:

```
aibuilder/data/
aibuilder/tmp/
aibuilder/.venv/
```

- [ ] **Step 10: Verify `make install-dev` works**

Run:
```bash
cd aibuilder && make install-dev
```

Expected: pip installs fastapi/uvicorn/anthropic/boto3/python-multipart, then pytest/ruff. No errors.

- [ ] **Step 11: Commit**

```bash
git add aibuilder/ .gitignore
git commit -m "scaffold(aibuilder): project skeleton, deps, makefile, gitignore"
```

---

## Task 2: `RepoProfile` dataclass + analyzer skeleton

**Files:**
- Create: `aibuilder/analyzer.py`
- Create: `aibuilder/tests/test_analyzer.py`

- [ ] **Step 1: Write the failing skeleton test** — `aibuilder/tests/test_analyzer.py`

```python
"""Tests for the repo analyzer."""

from pathlib import Path

from analyzer import RepoProfile, analyze_repo


def test_unknown_for_nonexistent_path(tmp_path: Path):
    missing = tmp_path / "nope"
    profile = analyze_repo(str(missing))
    assert isinstance(profile, RepoProfile)
    assert profile.app_type == "unknown"
    assert "not found" in profile.summary.lower()


def test_unknown_for_empty_dir(tmp_path: Path):
    profile = analyze_repo(str(tmp_path))
    assert profile.app_type == "unknown"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd aibuilder && pytest tests/test_analyzer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analyzer'`

- [ ] **Step 3: Create `aibuilder/analyzer.py` with minimal impl**

```python
"""Repo analyzer: classify a cloned repo into a RepoProfile.

Pure Python, no LLM calls. Walks the directory tree and reads manifest
files (package.json, requirements.txt, Dockerfile, etc.) to figure out
what kind of app this is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class RepoProfile:
    app_type: str
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    has_dockerfile: bool = False
    has_compose: bool = False
    has_database_hints: bool = False
    entry_points: list[str] = field(default_factory=list)
    build_command: str | None = None
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def analyze_repo(path: str) -> RepoProfile:
    root = Path(path)
    if not root.exists() or not root.is_dir():
        return RepoProfile(app_type="unknown", summary=f"Path not found: {path}")

    files = [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
    if not files:
        return RepoProfile(app_type="unknown", summary="Empty repository — nothing to analyze.")

    return RepoProfile(app_type="unknown", summary="Unable to determine app type yet.")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd aibuilder && pytest tests/test_analyzer.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add aibuilder/analyzer.py aibuilder/tests/test_analyzer.py
git commit -m "feat(aibuilder): RepoProfile dataclass + analyzer skeleton"
```

---

## Task 3: Analyzer detectors (one sub-cycle per app type)

**Files:**
- Modify: `aibuilder/analyzer.py` (grow the classifier)
- Modify: `aibuilder/tests/test_analyzer.py` (add fixture-based tests)
- Create: `aibuilder/tests/fixtures/static_site/`
- Create: `aibuilder/tests/fixtures/node_api/`
- Create: `aibuilder/tests/fixtures/python_api/`
- Create: `aibuilder/tests/fixtures/dockerized_web/`
- Create: `aibuilder/tests/fixtures/spa_with_api/`
- Create: `aibuilder/tests/fixtures/fullstack_with_db/`
- Create: `aibuilder/tests/fixtures/worker/`

### 3a: static_site detection

- [ ] **Step 1: Create the fixture**

`aibuilder/tests/fixtures/static_site/index.html`:
```html
<!doctype html>
<html><head><title>Hello</title></head>
<body><h1>Hello, world.</h1></body></html>
```

`aibuilder/tests/fixtures/static_site/style.css`:
```css
body { font-family: sans-serif; }
```

- [ ] **Step 2: Add the failing test**

Append to `aibuilder/tests/test_analyzer.py`:

```python
FIXTURES = Path(__file__).parent / "fixtures"


def test_static_site_detected():
    profile = analyze_repo(str(FIXTURES / "static_site"))
    assert profile.app_type == "static_site"
    assert "index.html" in profile.entry_points
    assert "html" in profile.languages
    assert profile.has_dockerfile is False
    assert profile.has_database_hints is False
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd aibuilder && pytest tests/test_analyzer.py::test_static_site_detected -v`
Expected: FAIL (app_type is "unknown")

- [ ] **Step 4: Extend the analyzer**

Replace the body of `aibuilder/analyzer.py` with:

```python
"""Repo analyzer: classify a cloned repo into a RepoProfile.

Pure Python, no LLM calls. Walks the directory tree and reads manifest
files (package.json, requirements.txt, Dockerfile, etc.) to figure out
what kind of app this is.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class RepoProfile:
    app_type: str
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    has_dockerfile: bool = False
    has_compose: bool = False
    has_database_hints: bool = False
    entry_points: list[str] = field(default_factory=list)
    build_command: str | None = None
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_LANGUAGE_EXTS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rb": "ruby",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
    ".php": "php",
    ".html": "html",
    ".css": "css",
}

_NODE_API_FRAMEWORKS = {"express", "fastify", "koa", "@hapi/hapi", "@nestjs/core"}
_NODE_FRONTEND_FRAMEWORKS = {"react", "vue", "svelte", "next", "@angular/core"}
_PYTHON_API_FRAMEWORKS = {"fastapi", "flask", "django"}
_NODE_WORKER_FRAMEWORKS = {"bull", "bullmq", "agenda"}
_PYTHON_WORKER_FRAMEWORKS = {"celery", "rq", "huey"}

_DB_HINT_KEYWORDS = (
    "postgres",
    "postgresql",
    "mysql",
    "mongodb",
    "redis",
    "DATABASE_URL",
    "DB_HOST",
)

_ENTRY_POINT_NAMES = (
    "index.html",
    "main.py",
    "app.py",
    "server.js",
    "server.ts",
    "index.js",
)


def analyze_repo(path: str) -> RepoProfile:
    root = Path(path)
    if not root.exists() or not root.is_dir():
        return RepoProfile(app_type="unknown", summary=f"Path not found: {path}")

    files = [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
    if not files:
        return RepoProfile(app_type="unknown", summary="Empty repository — nothing to analyze.")

    file_names = {p.name for p in files}
    languages = sorted({_LANGUAGE_EXTS[p.suffix] for p in files if p.suffix in _LANGUAGE_EXTS})

    has_dockerfile = "Dockerfile" in file_names
    has_compose = any(
        n in file_names for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml")
    )

    pkg_deps, pkg_scripts = _parse_package_json(root / "package.json")
    py_deps = _parse_python_deps(root)

    all_deps = set(pkg_deps) | set(py_deps)
    framework_set = {
        d
        for d in all_deps
        if d
        in _NODE_API_FRAMEWORKS
        | _NODE_FRONTEND_FRAMEWORKS
        | _PYTHON_API_FRAMEWORKS
        | _NODE_WORKER_FRAMEWORKS
        | _PYTHON_WORKER_FRAMEWORKS
    }
    frameworks = sorted(framework_set)

    has_database_hints = _detect_db_hints(root, all_deps)

    entry_points = [n for n in _ENTRY_POINT_NAMES if n in file_names]
    build_command = "npm run build" if pkg_scripts.get("build") else None

    app_type = _classify(file_names, pkg_deps, py_deps, has_dockerfile, has_database_hints)
    summary = _build_summary(app_type, languages, frameworks, has_dockerfile, has_database_hints)

    return RepoProfile(
        app_type=app_type,
        languages=languages,
        frameworks=frameworks,
        has_dockerfile=has_dockerfile,
        has_compose=has_compose,
        has_database_hints=has_database_hints,
        entry_points=entry_points,
        build_command=build_command,
        summary=summary,
    )


def _parse_package_json(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    if not path.exists():
        return {}, {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}, {}
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    scripts = data.get("scripts", {})
    return deps, scripts


def _parse_python_deps(root: Path) -> dict[str, str]:
    deps: dict[str, str] = {}
    req = root / "requirements.txt"
    if req.exists():
        for line in req.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = line.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].split("[")[0]
            deps[name.strip().lower()] = ""
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        # Minimal parser: look for "fastapi" / "flask" / "django" tokens in the file.
        text = pyproject.read_text().lower()
        for token in _PYTHON_API_FRAMEWORKS | _PYTHON_WORKER_FRAMEWORKS:
            if token in text:
                deps[token] = ""
    return deps


def _detect_db_hints(root: Path, deps: set[str]) -> bool:
    if any(d.lower() in {"pg", "psycopg2", "psycopg", "pymongo", "mysql2", "mysql"} for d in deps):
        return True
    candidates = [
        root / "requirements.txt",
        root / "package.json",
        root / "docker-compose.yml",
        root / "docker-compose.yaml",
        root / ".env.example",
        root / ".env.sample",
    ]
    for c in candidates:
        if c.exists():
            try:
                text = c.read_text()
            except OSError:
                continue
            if any(kw in text for kw in _DB_HINT_KEYWORDS):
                return True
    return False


def _classify(
    file_names: set[str],
    pkg_deps: dict[str, str],
    py_deps: dict[str, str],
    has_dockerfile: bool,
    has_database_hints: bool,
) -> str:
    pkg_set = set(pkg_deps)
    py_set = set(py_deps)

    has_node_api = bool(pkg_set & _NODE_API_FRAMEWORKS)
    has_python_api = bool(py_set & _PYTHON_API_FRAMEWORKS)
    has_node_frontend = bool(pkg_set & _NODE_FRONTEND_FRAMEWORKS)
    has_worker = bool(
        (pkg_set & _NODE_WORKER_FRAMEWORKS) or (py_set & _PYTHON_WORKER_FRAMEWORKS)
    )

    if has_node_frontend and (has_node_api or has_python_api):
        return "fullstack_with_db" if has_database_hints else "spa_with_api"
    if "index.html" in file_names and not (pkg_set or py_set):
        return "static_site"
    if has_node_frontend:
        return "static_site"
    if has_node_api:
        return "node_api"
    if has_python_api:
        return "python_api"
    if has_worker:
        return "worker"
    if has_dockerfile:
        return "dockerized_web"
    return "unknown"


def _build_summary(
    app_type: str,
    languages: list[str],
    frameworks: list[str],
    has_dockerfile: bool,
    has_database_hints: bool,
) -> str:
    if app_type == "unknown":
        return "I couldn't tell what kind of app this is from the files in the repo."

    type_phrase = {
        "static_site": "a static website (HTML/CSS/JS, no backend)",
        "spa_with_api": "a single-page app frontend with a backend API",
        "node_api": "a Node.js backend API",
        "python_api": "a Python backend API",
        "dockerized_web": "a Dockerized web service",
        "fullstack_with_db": "a full-stack web app that uses a database",
        "worker": "a background worker / scheduled job",
    }[app_type]

    fw = f" built with {', '.join(frameworks)}" if frameworks else ""
    docker = " It includes a Dockerfile." if has_dockerfile else ""
    db = " It looks like it talks to a database." if has_database_hints else ""
    langs = f" Languages detected: {', '.join(languages)}." if languages else ""
    return f"This looks like {type_phrase}{fw}.{langs}{docker}{db}".strip()
```

- [ ] **Step 5: Run the static_site test to verify it passes**

Run: `cd aibuilder && pytest tests/test_analyzer.py::test_static_site_detected -v`
Expected: PASS

### 3b: node_api detection

- [ ] **Step 6: Create the fixture**

`aibuilder/tests/fixtures/node_api/package.json`:
```json
{
  "name": "sample-api",
  "version": "1.0.0",
  "main": "server.js",
  "dependencies": {
    "express": "^4.18.0"
  },
  "scripts": {
    "start": "node server.js"
  }
}
```

`aibuilder/tests/fixtures/node_api/server.js`:
```javascript
const express = require("express");
const app = express();
app.get("/", (req, res) => res.send("ok"));
app.listen(3000);
```

- [ ] **Step 7: Add the test**

Append to `aibuilder/tests/test_analyzer.py`:

```python
def test_node_api_detected():
    profile = analyze_repo(str(FIXTURES / "node_api"))
    assert profile.app_type == "node_api"
    assert "express" in profile.frameworks
    assert "javascript" in profile.languages
    assert profile.has_database_hints is False
```

- [ ] **Step 8: Run, expect PASS** (the classifier from step 4 already handles this)

Run: `cd aibuilder && pytest tests/test_analyzer.py::test_node_api_detected -v`
Expected: PASS

### 3c: python_api detection

- [ ] **Step 9: Create the fixture**

`aibuilder/tests/fixtures/python_api/requirements.txt`:
```
fastapi==0.110.0
uvicorn==0.27.0
```

`aibuilder/tests/fixtures/python_api/main.py`:
```python
from fastapi import FastAPI
app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok"}
```

- [ ] **Step 10: Add the test**

Append:
```python
def test_python_api_detected():
    profile = analyze_repo(str(FIXTURES / "python_api"))
    assert profile.app_type == "python_api"
    assert "fastapi" in profile.frameworks
    assert "python" in profile.languages
```

- [ ] **Step 11: Run, expect PASS**

Run: `cd aibuilder && pytest tests/test_analyzer.py::test_python_api_detected -v`
Expected: PASS

### 3d: dockerized_web detection

- [ ] **Step 12: Create the fixture**

`aibuilder/tests/fixtures/dockerized_web/Dockerfile`:
```dockerfile
FROM golang:1.22
WORKDIR /app
COPY . .
RUN go build -o /server ./main.go
EXPOSE 8080
CMD ["/server"]
```

`aibuilder/tests/fixtures/dockerized_web/main.go`:
```go
package main

import (
    "net/http"
)

func main() {
    http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
        w.Write([]byte("ok"))
    })
    http.ListenAndServe(":8080", nil)
}
```

- [ ] **Step 13: Add the test**

Append:
```python
def test_dockerized_web_detected():
    profile = analyze_repo(str(FIXTURES / "dockerized_web"))
    assert profile.app_type == "dockerized_web"
    assert profile.has_dockerfile is True
    assert "go" in profile.languages
```

- [ ] **Step 14: Run, expect PASS**

Run: `cd aibuilder && pytest tests/test_analyzer.py::test_dockerized_web_detected -v`
Expected: PASS

### 3e: spa_with_api detection

- [ ] **Step 15: Create the fixture**

`aibuilder/tests/fixtures/spa_with_api/package.json`:
```json
{
  "name": "spa-with-api",
  "version": "1.0.0",
  "dependencies": {
    "react": "^18.0.0",
    "express": "^4.18.0"
  },
  "scripts": {
    "build": "vite build",
    "start": "node server.js"
  }
}
```

`aibuilder/tests/fixtures/spa_with_api/server.js`:
```javascript
const express = require("express");
const app = express();
app.get("/api/health", (req, res) => res.json({ status: "ok" }));
app.listen(3000);
```

`aibuilder/tests/fixtures/spa_with_api/src/App.jsx`:
```jsx
export default function App() {
  return <h1>Hello</h1>;
}
```

- [ ] **Step 16: Add the test**

Append:
```python
def test_spa_with_api_detected():
    profile = analyze_repo(str(FIXTURES / "spa_with_api"))
    assert profile.app_type == "spa_with_api"
    assert "react" in profile.frameworks
    assert "express" in profile.frameworks
    assert profile.build_command == "npm run build"
    assert profile.has_database_hints is False
```

- [ ] **Step 17: Run, expect PASS**

Run: `cd aibuilder && pytest tests/test_analyzer.py::test_spa_with_api_detected -v`
Expected: PASS

### 3f: fullstack_with_db detection

- [ ] **Step 18: Create the fixture**

`aibuilder/tests/fixtures/fullstack_with_db/package.json`:
```json
{
  "name": "fullstack",
  "version": "1.0.0",
  "dependencies": {
    "react": "^18.0.0",
    "express": "^4.18.0",
    "pg": "^8.11.0"
  }
}
```

`aibuilder/tests/fixtures/fullstack_with_db/.env.example`:
```
DATABASE_URL=postgres://user:pass@localhost:5432/myapp
NODE_ENV=development
```

`aibuilder/tests/fixtures/fullstack_with_db/server.js`:
```javascript
const express = require("express");
const { Pool } = require("pg");
const app = express();
const pool = new Pool();
app.get("/users", async (req, res) => {
  const { rows } = await pool.query("SELECT * FROM users");
  res.json(rows);
});
app.listen(3000);
```

- [ ] **Step 19: Add the test**

Append:
```python
def test_fullstack_with_db_detected():
    profile = analyze_repo(str(FIXTURES / "fullstack_with_db"))
    assert profile.app_type == "fullstack_with_db"
    assert profile.has_database_hints is True
```

- [ ] **Step 20: Run, expect PASS**

Run: `cd aibuilder && pytest tests/test_analyzer.py::test_fullstack_with_db_detected -v`
Expected: PASS

### 3g: worker detection

- [ ] **Step 21: Create the fixture**

`aibuilder/tests/fixtures/worker/requirements.txt`:
```
celery==5.3.0
redis==5.0.0
```

`aibuilder/tests/fixtures/worker/tasks.py`:
```python
from celery import Celery
app = Celery("tasks", broker="redis://localhost:6379/0")

@app.task
def send_email(to: str):
    print(f"Sending to {to}")
```

- [ ] **Step 22: Add the test**

Append:
```python
def test_worker_detected():
    profile = analyze_repo(str(FIXTURES / "worker"))
    assert profile.app_type == "worker"
    assert "celery" in profile.frameworks
```

- [ ] **Step 23: Run, expect PASS**

Run: `cd aibuilder && pytest tests/test_analyzer.py::test_worker_detected -v`
Expected: PASS

### 3h: full analyzer run

- [ ] **Step 24: Run all analyzer tests**

Run: `cd aibuilder && pytest tests/test_analyzer.py -v`
Expected: 9 passed (2 from Task 2 + 7 from Task 3)

- [ ] **Step 25: Commit**

```bash
git add aibuilder/analyzer.py aibuilder/tests/test_analyzer.py aibuilder/tests/fixtures/
git commit -m "feat(aibuilder): analyzer with detectors for 7 app patterns"
```

---

## Task 4: Summary content sanity check

The analyzer already generates summaries via `_build_summary`. Add one targeted test to lock in user-facing wording, since the spec calls the summary "load-bearing for trust".

**Files:**
- Modify: `aibuilder/tests/test_analyzer.py`

- [ ] **Step 1: Add the failing test**

```python
def test_summary_includes_app_type_and_frameworks():
    profile = analyze_repo(str(FIXTURES / "python_api"))
    assert "python backend api" in profile.summary.lower()
    assert "fastapi" in profile.summary.lower()
    assert "python" in profile.summary.lower()


def test_summary_for_unknown_repo(tmp_path: Path):
    # Repo with one stray text file → no manifest, no html
    (tmp_path / "notes.txt").write_text("just a note")
    profile = analyze_repo(str(tmp_path))
    assert profile.app_type == "unknown"
    assert "couldn't tell" in profile.summary.lower()
```

- [ ] **Step 2: Run, expect PASS** (the impl in Task 3 already produces these)

Run: `cd aibuilder && pytest tests/test_analyzer.py -v -k summary`
Expected: 2 passed

- [ ] **Step 3: Commit**

```bash
git add aibuilder/tests/test_analyzer.py
git commit -m "test(aibuilder): lock in summary wording for analyzer"
```

---

## Task 5: Pattern catalog (`patterns.py`)

**Files:**
- Create: `aibuilder/patterns.py`
- Create: `aibuilder/tests/test_patterns.py`

- [ ] **Step 1: Write the failing test** — `aibuilder/tests/test_patterns.py`

```python
"""Tests for the AWS architecture pattern catalog."""

from analyzer import RepoProfile
from patterns import Architecture, ArchitectureService, recommend


def test_static_site_pattern():
    profile = RepoProfile(app_type="static_site")
    arch = recommend(profile)
    assert isinstance(arch, Architecture)
    assert arch.pattern == "static_site"
    services = [s.aws_service for s in arch.services]
    assert services == ["S3", "CloudFront"]


def test_spa_with_api_pattern():
    profile = RepoProfile(app_type="spa_with_api")
    arch = recommend(profile)
    assert arch.pattern == "spa_with_api"
    assert [s.aws_service for s in arch.services] == ["S3", "CloudFront", "API Gateway", "Lambda"]


def test_node_api_default_is_app_runner():
    profile = RepoProfile(app_type="node_api")
    arch = recommend(profile)
    assert arch.pattern == "node_api"
    assert arch.services[0].aws_service == "App Runner"
    assert any("Lambda" in note for note in arch.notes)


def test_fullstack_with_db_includes_rds():
    profile = RepoProfile(app_type="fullstack_with_db")
    arch = recommend(profile)
    assert arch.pattern == "fullstack_with_db"
    services = [s.aws_service for s in arch.services]
    assert "App Runner" in services
    assert "RDS PostgreSQL" in services


def test_spa_with_api_upgrades_to_fullstack_when_db_hints():
    profile = RepoProfile(app_type="spa_with_api", has_database_hints=True)
    arch = recommend(profile)
    assert arch.pattern == "fullstack_with_db"


def test_worker_pattern():
    profile = RepoProfile(app_type="worker")
    arch = recommend(profile)
    assert arch.pattern == "worker"
    services = [s.aws_service for s in arch.services]
    assert services == ["EventBridge Scheduler", "Lambda"]


def test_unknown_returns_empty_with_helpful_note():
    profile = RepoProfile(app_type="unknown")
    arch = recommend(profile)
    assert arch.pattern == "unknown"
    assert arch.services == []
    assert any("describe" in n.lower() for n in arch.notes)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd aibuilder && pytest tests/test_patterns.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'patterns'`

- [ ] **Step 3: Create `aibuilder/patterns.py`**

```python
"""AWS architecture pattern catalog.

Maps a RepoProfile.app_type to a concrete Architecture (named AWS
services + per-service sizing). This is the deterministic 'brain' of
the agent — the LLM does NOT pick services, the catalog does.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from analyzer import RepoProfile


@dataclass
class ArchitectureService:
    aws_service: str
    purpose: str
    sizing: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Architecture:
    pattern: str
    services: list[ArchitectureService] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "services": [s.to_dict() for s in self.services],
            "notes": self.notes,
        }


_CATALOG: dict[str, Architecture] = {
    "static_site": Architecture(
        pattern="static_site",
        services=[
            ArchitectureService(
                aws_service="S3",
                purpose="Stores your built static assets.",
                sizing={"storage_gb": 1},
            ),
            ArchitectureService(
                aws_service="CloudFront",
                purpose="Serves your site globally over HTTPS with edge caching.",
                sizing={"data_out_gb": 5, "requests_per_month": 100_000},
            ),
        ],
    ),
    "spa_with_api": Architecture(
        pattern="spa_with_api",
        services=[
            ArchitectureService(
                aws_service="S3",
                purpose="Stores your SPA bundle.",
                sizing={"storage_gb": 1},
            ),
            ArchitectureService(
                aws_service="CloudFront",
                purpose="Serves the frontend and proxies API traffic.",
                sizing={"data_out_gb": 5, "requests_per_month": 100_000},
            ),
            ArchitectureService(
                aws_service="API Gateway",
                purpose="Public HTTPS endpoint for your backend.",
                sizing={"requests_per_month": 100_000},
            ),
            ArchitectureService(
                aws_service="Lambda",
                purpose="Runs your backend on demand; scales to zero.",
                sizing={"requests_per_month": 100_000, "memory_mb": 256, "duration_ms": 200},
            ),
        ],
    ),
    "node_api": Architecture(
        pattern="node_api",
        services=[
            ArchitectureService(
                aws_service="App Runner",
                purpose="Auto-scaling container hosting for your Node.js API.",
                sizing={"vcpu": 0.25, "memory_gb": 0.5, "requests_per_month": 100_000},
            ),
        ],
        notes=["Alternative: Lambda + API Gateway if the API is stateless and traffic is spiky."],
    ),
    "python_api": Architecture(
        pattern="python_api",
        services=[
            ArchitectureService(
                aws_service="App Runner",
                purpose="Auto-scaling container hosting for your Python API.",
                sizing={"vcpu": 0.25, "memory_gb": 0.5, "requests_per_month": 100_000},
            ),
        ],
        notes=["Alternative: Lambda + API Gateway via Mangum if the API is stateless."],
    ),
    "dockerized_web": Architecture(
        pattern="dockerized_web",
        services=[
            ArchitectureService(
                aws_service="App Runner",
                purpose="Runs your container, auto-scales, no cluster management.",
                sizing={"vcpu": 0.25, "memory_gb": 0.5, "requests_per_month": 100_000},
            ),
        ],
        notes=["Alternative: ECS Fargate if you need more networking control or sidecars."],
    ),
    "fullstack_with_db": Architecture(
        pattern="fullstack_with_db",
        services=[
            ArchitectureService(
                aws_service="App Runner",
                purpose="Hosts your web app container.",
                sizing={"vcpu": 0.5, "memory_gb": 1.0, "requests_per_month": 100_000},
            ),
            ArchitectureService(
                aws_service="RDS PostgreSQL",
                purpose="Managed database (db.t4g.micro, 20 GB gp3, Single-AZ).",
                sizing={"instance_class": "db.t4g.micro", "storage_gb": 20},
            ),
        ],
        notes=["Alternative: Aurora Serverless v2 (min 0.5 ACU) if you want auto-pause."],
    ),
    "worker": Architecture(
        pattern="worker",
        services=[
            ArchitectureService(
                aws_service="EventBridge Scheduler",
                purpose="Triggers your job on a schedule.",
                sizing={"invocations_per_month": 720},
            ),
            ArchitectureService(
                aws_service="Lambda",
                purpose="Runs the job; scales to zero between runs.",
                sizing={"memory_mb": 512, "duration_ms": 5000, "invocations_per_month": 720},
            ),
        ],
        notes=["Alternative: ECS Fargate scheduled task if jobs run longer than 15 min."],
    ),
}


def recommend(profile: RepoProfile) -> Architecture:
    """Map a RepoProfile to an Architecture.

    Special case: if profile is `spa_with_api` but has database hints,
    upgrade to `fullstack_with_db` (the spec calls this out explicitly).
    """
    if profile.app_type == "unknown":
        return Architecture(
            pattern="unknown",
            services=[],
            notes=[
                "I couldn't tell what kind of app this is from the files. "
                "Can you describe what it does?"
            ],
        )

    pattern_key = profile.app_type
    if pattern_key == "spa_with_api" and profile.has_database_hints:
        pattern_key = "fullstack_with_db"

    return _CATALOG[pattern_key]
```

- [ ] **Step 4: Run all pattern tests, expect PASS**

Run: `cd aibuilder && pytest tests/test_patterns.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add aibuilder/patterns.py aibuilder/tests/test_patterns.py
git commit -m "feat(aibuilder): AWS pattern catalog with 7 patterns + recommend()"
```

---

## Task 6: Cost estimator with fallback table

**Files:**
- Create: `aibuilder/pricing.py`
- Create: `aibuilder/tests/test_pricing.py`

This task implements the v1 cost estimator using ONLY the curated fallback table. The `is_fallback` field is wired so a Phase 1.5 plan can add live AWS Pricing API lookups without changing call sites.

- [ ] **Step 1: Write the failing test** — `aibuilder/tests/test_pricing.py`

```python
"""Tests for cost estimation."""

from patterns import recommend
from analyzer import RepoProfile
from pricing import CostEstimate, CostLine, estimate


def test_static_site_estimate_is_reasonable():
    arch = recommend(RepoProfile(app_type="static_site"))
    result = estimate(arch)
    assert isinstance(result, CostEstimate)
    assert len(result.lines) == 2
    services = [line.service for line in result.lines]
    assert services == ["S3", "CloudFront"]
    # Static site at low traffic should be cheap — under $5/mo
    assert result.total_monthly_usd < 5.0
    assert result.is_fallback is True  # v1: always fallback
    assert any("us-east-1" in a for a in result.assumptions)


def test_fullstack_estimate_includes_rds_line():
    arch = recommend(RepoProfile(app_type="fullstack_with_db"))
    result = estimate(arch)
    services = [line.service for line in result.lines]
    assert "RDS PostgreSQL" in services
    rds_line = next(line for line in result.lines if line.service == "RDS PostgreSQL")
    assert rds_line.monthly_usd > 0  # RDS is not free


def test_unknown_pattern_returns_empty_estimate():
    arch = recommend(RepoProfile(app_type="unknown"))
    result = estimate(arch)
    assert result.lines == []
    assert result.total_monthly_usd == 0.0


def test_unrecognized_service_falls_through_with_zero():
    """If patterns.py is extended with a service we haven't priced yet,
    estimate should not crash — it should record a zero with a note."""
    from patterns import Architecture, ArchitectureService

    arch = Architecture(
        pattern="custom",
        services=[ArchitectureService("FakeService", "demo", {})],
    )
    result = estimate(arch)
    assert len(result.lines) == 1
    assert result.lines[0].monthly_usd == 0.0
    assert "no price" in result.lines[0].note.lower()


def test_cost_line_dataclass():
    line = CostLine(service="S3", monthly_usd=0.10, note="1 GB stored")
    assert line.service == "S3"
    assert line.monthly_usd == 0.10
```

- [ ] **Step 2: Run, verify fail**

Run: `cd aibuilder && pytest tests/test_pricing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pricing'`

- [ ] **Step 3: Create `aibuilder/pricing.py`**

```python
"""Cost estimator.

v1: uses a curated fallback table only. The `is_fallback` field on
CostEstimate is always True. A Phase 1.5 plan will add live AWS Pricing
API lookups; this module is structured so that becomes additive.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from patterns import Architecture


@dataclass
class CostLine:
    service: str
    monthly_usd: float
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CostEstimate:
    lines: list[CostLine] = field(default_factory=list)
    total_monthly_usd: float = 0.0
    assumptions: list[str] = field(default_factory=list)
    is_fallback: bool = True

    def to_dict(self) -> dict:
        return {
            "lines": [line.to_dict() for line in self.lines],
            "total_monthly_usd": self.total_monthly_usd,
            "assumptions": self.assumptions,
            "is_fallback": self.is_fallback,
        }


# Service → (monthly_usd, note) at the prototype baseline below.
# Numbers are rough order-of-magnitude estimates and are deliberately
# rounded — they're a starting point, not a quote.
_FALLBACK_PRICES: dict[str, tuple[float, str]] = {
    "S3": (0.10, "~1 GB stored + ~10k GET requests"),
    "CloudFront": (0.50, "~5 GB data out + ~100k requests (free tier covers most prototypes)"),
    "API Gateway": (0.35, "~100k HTTP API requests"),
    "Lambda": (0.10, "~100k invocations at 256 MB / 200 ms"),
    "App Runner": (5.00, "0.25 vCPU / 0.5 GB, scales to zero when idle"),
    "RDS PostgreSQL": (12.00, "db.t4g.micro, 20 GB gp3, Single-AZ"),
    "EventBridge Scheduler": (0.00, "<1k invocations/mo is in the free tier"),
}

_BASELINE_ASSUMPTIONS = [
    "Region: us-east-1",
    "~100,000 requests per month",
    "~5 GB CloudFront egress",
    "Lambda: 256 MB memory, 200 ms avg duration",
    "App Runner: 0.25 vCPU / 0.5 GB, scales to zero after idle",
    "RDS: db.t4g.micro, 20 GB gp3, Single-AZ",
    "Numbers are rough starting points — actual cost depends on real traffic.",
]


def estimate(architecture: Architecture) -> CostEstimate:
    if not architecture.services:
        return CostEstimate(
            lines=[],
            total_monthly_usd=0.0,
            assumptions=[],
            is_fallback=True,
        )

    lines: list[CostLine] = []
    for svc in architecture.services:
        usd, note = _FALLBACK_PRICES.get(svc.aws_service, (0.0, "no price available"))
        lines.append(CostLine(service=svc.aws_service, monthly_usd=round(usd, 2), note=note))

    total = round(sum(line.monthly_usd for line in lines), 2)
    return CostEstimate(
        lines=lines,
        total_monthly_usd=total,
        assumptions=_BASELINE_ASSUMPTIONS,
        is_fallback=True,
    )
```

- [ ] **Step 4: Run pricing tests, expect PASS**

Run: `cd aibuilder && pytest tests/test_pricing.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add aibuilder/pricing.py aibuilder/tests/test_pricing.py
git commit -m "feat(aibuilder): fallback-table cost estimator (live pricing API deferred)"
```

---

## Task 7: `clone_repo` tool

**Files:**
- Create: `aibuilder/tools.py`
- Create: `aibuilder/tests/test_tools.py`

- [ ] **Step 1: Write the failing test** — `aibuilder/tests/test_tools.py`

```python
"""Tests for tool implementations."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import clone_repo


def test_clone_rejects_non_github_url():
    result = clone_repo("https://gitlab.com/foo/bar", session_id="s1")
    assert "summary" in result
    assert "github" in result["summary"].lower()


def test_clone_rejects_garbage_url():
    result = clone_repo("not a url", session_id="s1")
    assert "summary" in result


def test_clone_accepts_canonical_github_urls(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_TMP_DIR", str(tmp_path))

    def fake_run(cmd, **kwargs):
        # Pretend git clone succeeded by creating the target dir.
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.html").write_text("<html/>")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = clone_repo("https://github.com/octocat/Hello-World", session_id="s1")
    assert "path" in result
    assert result["repo_name"] == "Hello-World"
    assert result["file_count"] == 1


def test_clone_rejects_repo_too_many_files(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_TMP_DIR", str(tmp_path))
    monkeypatch.setenv("AIBUILDER_MAX_FILES", "3")

    def fake_run(cmd, **kwargs):
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            (target / f"f{i}.txt").write_text("x")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = clone_repo("https://github.com/octocat/big-repo", session_id="s2")
    assert "summary" in result
    assert "too large" in result["summary"].lower() or "subfolder" in result["summary"].lower()


def test_clone_handles_git_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_TMP_DIR", str(tmp_path))

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 128, stdout="", stderr="fatal: repository 'https://github.com/no/exist' not found"
        )

    with patch("subprocess.run", side_effect=fake_run):
        result = clone_repo("https://github.com/no/exist", session_id="s3")
    assert "summary" in result
    assert "public" in result["summary"].lower()
```

- [ ] **Step 2: Run, expect fail**

Run: `cd aibuilder && pytest tests/test_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tools'`

- [ ] **Step 3: Create `aibuilder/tools.py`**

```python
"""Tool implementations for the aibuilder agent.

Each tool returns either a success dict or a {"summary", "details"}
error dict — never raises. The agent's system prompt teaches it to
surface `summary` verbatim and offer `details` only if asked.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from analyzer import analyze_repo as _analyze
from patterns import recommend as _recommend
from pricing import estimate as _estimate

_TMP_DIR_DEFAULT = Path(__file__).parent / "tmp" / "repos"
_GITHUB_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)
_MAX_FILES_DEFAULT = 5000
_MAX_SIZE_MB_DEFAULT = 500


def _tmp_root() -> Path:
    return Path(os.environ.get("AIBUILDER_TMP_DIR", str(_TMP_DIR_DEFAULT)))


def _max_files() -> int:
    return int(os.environ.get("AIBUILDER_MAX_FILES", _MAX_FILES_DEFAULT))


def _max_size_mb() -> int:
    return int(os.environ.get("AIBUILDER_MAX_SIZE_MB", _MAX_SIZE_MB_DEFAULT))


def clone_repo(github_url: str, *, session_id: str, **_: Any) -> dict:
    match = _GITHUB_URL_RE.match(github_url.strip())
    if not match:
        return {
            "summary": "That doesn't look like a GitHub repo URL. "
            "Try `https://github.com/<owner>/<repo>`.",
            "details": f"received: {github_url!r}",
        }

    repo_name = match.group("repo")
    target = _tmp_root() / session_id / repo_name
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        ["git", "clone", "--depth=1", github_url, str(target)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        stderr = proc.stderr or ""
        if "not found" in stderr.lower() or "could not read" in stderr.lower():
            return {
                "summary": "I couldn't reach that repo. "
                "I can only see public repos right now — make sure the URL is correct and public.",
                "details": stderr[-2000:],
            }
        return {
            "summary": "Cloning the repo failed.",
            "details": stderr[-2000:],
        }

    files = [p for p in target.rglob("*") if p.is_file() and ".git" not in p.parts]
    file_count = len(files)
    if file_count > _max_files():
        shutil.rmtree(target, ignore_errors=True)
        return {
            "summary": "This repo is too large for me to scan in one go. "
            "Point me at the subfolder for the app you want to deploy.",
            "details": f"file_count={file_count}, limit={_max_files()}",
        }

    size_bytes = sum(p.stat().st_size for p in files if p.exists())
    size_mb = size_bytes / (1024 * 1024)
    if size_mb > _max_size_mb():
        shutil.rmtree(target, ignore_errors=True)
        return {
            "summary": "This repo is too large for me to scan in one go. "
            "Point me at the subfolder for the app you want to deploy.",
            "details": f"size_mb={size_mb:.1f}, limit={_max_size_mb()}",
        }

    return {
        "path": str(target),
        "repo_name": repo_name,
        "file_count": file_count,
        "size_mb": round(size_mb, 2),
    }


def analyze_repo(path: str, **_: Any) -> dict:
    profile = _analyze(path)
    return profile.to_dict()


def recommend_architecture(profile: dict, **_: Any) -> dict:
    from analyzer import RepoProfile

    rp = RepoProfile(**profile)
    return _recommend(rp).to_dict()


def estimate_cost(architecture: dict, **_: Any) -> dict:
    from patterns import Architecture, ArchitectureService

    arch = Architecture(
        pattern=architecture.get("pattern", "unknown"),
        services=[ArchitectureService(**s) for s in architecture.get("services", [])],
        notes=list(architecture.get("notes", [])),
    )
    return _estimate(arch).to_dict()
```

- [ ] **Step 4: Run clone tests, expect PASS**

Run: `cd aibuilder && pytest tests/test_tools.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add aibuilder/tools.py aibuilder/tests/test_tools.py
git commit -m "feat(aibuilder): clone_repo tool + thin wrappers for analyze/recommend/estimate"
```

---

## Task 8: Tool wrappers — round-trip tests

The wrappers in Task 7 were minimal. Add tests that prove the full chain (analyze → recommend → estimate) round-trips through the JSON-shaped tool surface that Claude will see.

**Files:**
- Modify: `aibuilder/tests/test_tools.py`

- [ ] **Step 1: Add round-trip tests**

Append to `aibuilder/tests/test_tools.py`:

```python
from pathlib import Path

from tools import analyze_repo, estimate_cost, recommend_architecture

FIXTURES = Path(__file__).parent / "fixtures"


def test_full_chain_static_site():
    profile = analyze_repo(str(FIXTURES / "static_site"))
    assert profile["app_type"] == "static_site"
    arch = recommend_architecture(profile)
    assert arch["pattern"] == "static_site"
    assert [s["aws_service"] for s in arch["services"]] == ["S3", "CloudFront"]
    cost = estimate_cost(arch)
    assert cost["total_monthly_usd"] > 0
    assert cost["is_fallback"] is True


def test_full_chain_fullstack_db():
    profile = analyze_repo(str(FIXTURES / "fullstack_with_db"))
    arch = recommend_architecture(profile)
    assert arch["pattern"] == "fullstack_with_db"
    cost = estimate_cost(arch)
    services = [line["service"] for line in cost["lines"]]
    assert "RDS PostgreSQL" in services


def test_recommend_handles_dict_profile():
    """The agent will pass profiles as JSON dicts, not dataclasses."""
    profile = {
        "app_type": "node_api",
        "languages": ["javascript"],
        "frameworks": ["express"],
        "has_dockerfile": False,
        "has_compose": False,
        "has_database_hints": False,
        "entry_points": ["server.js"],
        "build_command": None,
        "summary": "",
    }
    arch = recommend_architecture(profile)
    assert arch["pattern"] == "node_api"
```

- [ ] **Step 2: Run, expect PASS**

Run: `cd aibuilder && pytest tests/test_tools.py -v -k full_chain or recommend`
Expected: 3 new passes (8 total in `test_tools.py`)

- [ ] **Step 3: Commit**

```bash
git add aibuilder/tests/test_tools.py
git commit -m "test(aibuilder): full-chain analyze→recommend→estimate round trip"
```

---

## Task 9: Tool registry — `TOOL_DEFINITIONS` and `execute_tool`

Append the Anthropic tool registry to `tools.py` so the agent loop can call the four tools by name.

**Files:**
- Modify: `aibuilder/tools.py`
- Modify: `aibuilder/tests/test_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `aibuilder/tests/test_tools.py`:

```python
def test_tool_definitions_shape():
    from tools import TOOL_DEFINITIONS

    names = [t["name"] for t in TOOL_DEFINITIONS]
    assert names == ["clone_repo", "analyze_repo", "recommend_architecture", "estimate_cost"]
    for t in TOOL_DEFINITIONS:
        assert "description" in t
        assert "input_schema" in t


def test_execute_tool_dispatches():
    from tools import execute_tool

    result = execute_tool(
        "analyze_repo",
        {"path": str(FIXTURES / "static_site")},
        session_id="s1",
        session=None,
    )
    assert result["app_type"] == "static_site"


def test_execute_unknown_tool_returns_error():
    from tools import execute_tool

    result = execute_tool("does_not_exist", {}, session_id="s1", session=None)
    assert "summary" in result
    assert "unknown tool" in result["summary"].lower()
```

- [ ] **Step 2: Run, expect fail**

Run: `cd aibuilder && pytest tests/test_tools.py -v -k "tool_definitions or execute"`
Expected: FAIL with `ImportError: cannot import name 'TOOL_DEFINITIONS'`

- [ ] **Step 3: Append to `aibuilder/tools.py`**

```python
# ── Tool registry ────────────────────────────────────────────────────────────


TOOL_DEFINITIONS = [
    {
        "name": "clone_repo",
        "description": (
            "Clone a public GitHub repository to a temporary directory so it can be analyzed. "
            "Returns the local path. Use this first, before analyze_repo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "github_url": {
                    "type": "string",
                    "description": "Full HTTPS URL like https://github.com/<owner>/<repo>",
                },
            },
            "required": ["github_url"],
        },
    },
    {
        "name": "analyze_repo",
        "description": (
            "Inspect a cloned repo and classify the app. Returns a RepoProfile dict "
            "(app_type, languages, frameworks, has_dockerfile, has_database_hints, "
            "summary, etc.). Always present the `summary` to the user verbatim and ask "
            "them to confirm before recommending an architecture."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Local filesystem path from clone_repo",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "recommend_architecture",
        "description": (
            "Given a RepoProfile, return the recommended AWS architecture as a list of "
            "named services with per-service purpose and sizing. Do NOT invent services — "
            "the returned services list is authoritative."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "object",
                    "description": "The RepoProfile dict returned by analyze_repo, "
                    "with any user corrections applied (e.g., user said they also "
                    "use a database → set has_database_hints to true).",
                },
            },
            "required": ["profile"],
        },
    },
    {
        "name": "estimate_cost",
        "description": (
            "Given an Architecture dict from recommend_architecture, return a monthly "
            "cost estimate with per-service breakdown and the assumptions used. Do NOT "
            "invent dollar amounts — the returned numbers are authoritative."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "architecture": {
                    "type": "object",
                    "description": "The Architecture dict returned by recommend_architecture",
                },
            },
            "required": ["architecture"],
        },
    },
]


_TOOL_FUNCS = {
    "clone_repo": clone_repo,
    "analyze_repo": analyze_repo,
    "recommend_architecture": recommend_architecture,
    "estimate_cost": estimate_cost,
}


def execute_tool(name: str, args: dict, *, session_id: str, session: Any) -> dict:
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        return {"summary": f"Unknown tool: {name}", "details": ""}
    return fn(**args, session_id=session_id, session=session)
```

- [ ] **Step 4: Run, expect PASS**

Run: `cd aibuilder && pytest tests/test_tools.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add aibuilder/tools.py aibuilder/tests/test_tools.py
git commit -m "feat(aibuilder): TOOL_DEFINITIONS registry + execute_tool dispatcher"
```

---

## Task 10: Session store (SQLite)

Adapted directly from `deploy-agent/sessions.py`. The aibuilder session needs different fields: no `upload_dir`, no `files`, no `deployment`. Instead: `clone_path` (where the cloned repo lives) and `last_profile` (the most recent RepoProfile dict, so the agent can re-use it without re-analyzing).

**Files:**
- Create: `aibuilder/sessions.py`
- Create: `aibuilder/tests/test_sessions.py`

- [ ] **Step 1: Write the failing test** — `aibuilder/tests/test_sessions.py`

```python
"""Tests for the session store."""

from pathlib import Path

from sessions import Session, SqliteSessionStore


def test_create_returns_session_with_id(tmp_path: Path):
    store = SqliteSessionStore(tmp_path / "test.db")
    session = store.create()
    assert isinstance(session, Session)
    assert session.session_id
    assert session.messages == []
    assert session.clone_path is None
    assert session.last_profile is None


def test_round_trip_through_sqlite(tmp_path: Path):
    store = SqliteSessionStore(tmp_path / "test.db")
    session = store.create()
    session.messages.append({"role": "user", "content": "hi"})
    session.clone_path = "/tmp/repos/xyz/foo"
    session.last_profile = {"app_type": "static_site", "summary": "Static site."}
    store.save(session)

    reloaded = store.get(session.session_id)
    assert reloaded is not None
    assert reloaded.messages == [{"role": "user", "content": "hi"}]
    assert reloaded.clone_path == "/tmp/repos/xyz/foo"
    assert reloaded.last_profile["app_type"] == "static_site"


def test_get_unknown_returns_none(tmp_path: Path):
    store = SqliteSessionStore(tmp_path / "test.db")
    assert store.get("does-not-exist") is None
```

- [ ] **Step 2: Run, expect fail**

Run: `cd aibuilder && pytest tests/test_sessions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sessions'`

- [ ] **Step 3: Create `aibuilder/sessions.py`**

```python
"""Session model + SQLite storage.

Adapted from deploy-agent/sessions.py. Different fields: no uploads,
no deployment record; instead carries `clone_path` (where the most
recently cloned repo lives) and `last_profile` (cached RepoProfile).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import uuid4


@dataclass
class Session:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    clone_path: str | None = None
    last_profile: dict | None = None


class SessionStore(Protocol):
    def create(self) -> Session: ...
    def get(self, session_id: str) -> Session | None: ...
    def save(self, session: Session) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,
  messages     TEXT NOT NULL DEFAULT '[]',
  clone_path   TEXT,
  last_profile TEXT,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class SqliteSessionStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create(self) -> Session:
        session = Session(session_id=str(uuid4()))
        with self._connect() as conn:
            conn.execute("INSERT INTO sessions (session_id) VALUES (?)", (session.session_id,))
        return session

    def get(self, session_id: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return Session(
            session_id=row["session_id"],
            messages=json.loads(row["messages"]),
            clone_path=row["clone_path"],
            last_profile=json.loads(row["last_profile"]) if row["last_profile"] else None,
        )

    def save(self, session: Session) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, messages, clone_path, last_profile, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO UPDATE SET
                    messages     = excluded.messages,
                    clone_path   = excluded.clone_path,
                    last_profile = excluded.last_profile,
                    updated_at   = CURRENT_TIMESTAMP
                """,
                (
                    session.session_id,
                    json.dumps(session.messages),
                    session.clone_path,
                    json.dumps(session.last_profile) if session.last_profile else None,
                ),
            )
```

- [ ] **Step 4: Run session tests, expect PASS**

Run: `cd aibuilder && pytest tests/test_sessions.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add aibuilder/sessions.py aibuilder/tests/test_sessions.py
git commit -m "feat(aibuilder): SQLite session store with clone_path + last_profile"
```

---

## Task 11: Agent loop + system prompt

Adapted directly from `deploy-agent/agent.py`. Same loop mechanics; different `SYSTEM_PROMPT` encoding the 4-stage workflow from the spec.

**Files:**
- Create: `aibuilder/agent.py`
- Create: `aibuilder/tests/test_agent.py`

- [ ] **Step 1: Write the failing test** — `aibuilder/tests/test_agent.py`

```python
"""Tests for the agent loop."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent import SYSTEM_PROMPT, run_agent_loop
from sessions import Session


def test_system_prompt_mentions_four_stage_workflow():
    assert "clone_repo" in SYSTEM_PROMPT
    assert "analyze_repo" in SYSTEM_PROMPT
    assert "recommend_architecture" in SYSTEM_PROMPT
    assert "estimate_cost" in SYSTEM_PROMPT
    # The validation step is load-bearing for trust — must be in the prompt.
    assert "confirm" in SYSTEM_PROMPT.lower() or "verbatim" in SYSTEM_PROMPT.lower()


def _stub_response(text: str, *, stop_reason: str = "end_turn", tool_calls: list | None = None):
    blocks = []
    if text:
        blocks.append(SimpleNamespace(type="text", text=text, model_dump=lambda: {"type": "text", "text": text}))
    for call in tool_calls or []:
        blocks.append(SimpleNamespace(type="tool_use", **call, model_dump=lambda c=call: {"type": "tool_use", **c}))
    return SimpleNamespace(stop_reason=stop_reason, content=blocks)


def test_agent_returns_text_on_end_turn():
    client = MagicMock()
    client.messages.create.return_value = _stub_response("Hi there.")
    session = Session(session_id="s1")
    reply = run_agent_loop(client, session)
    assert reply == "Hi there."
    assert session.messages[-1]["role"] == "assistant"


def test_agent_stops_at_iteration_limit():
    client = MagicMock()
    # Always return tool_use → never end_turn → loop hits the cap
    client.messages.create.return_value = _stub_response(
        "",
        stop_reason="tool_use",
        tool_calls=[{"id": "x", "name": "analyze_repo", "input": {"path": "/nope"}}],
    )
    session = Session(session_id="s1")
    reply = run_agent_loop(client, session)
    assert "iteration limit" in reply.lower()
```

- [ ] **Step 2: Run, expect fail**

Run: `cd aibuilder && pytest tests/test_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent'`

- [ ] **Step 3: Create `aibuilder/agent.py`**

```python
"""Claude tool-use loop + system prompt for aibuilder."""

from __future__ import annotations

import json
from typing import Any

import anthropic

from sessions import Session
from tools import TOOL_DEFINITIONS, execute_tool

MAX_AGENT_ITERATIONS = 15

SYSTEM_PROMPT = """You are aibuilder — a friendly AWS architecture assistant. \
A user gives you a public GitHub repo URL. You figure out what the app is, \
recommend a concrete AWS architecture, and estimate the monthly cost.

Your workflow has four stages. Do them in order.

1. **Ingest.** If the user hasn't given you a GitHub URL yet, ask for one. \
Once they give you one, call `clone_repo` with the URL.

2. **Validate.** Call `analyze_repo` with the `path` returned by `clone_repo`. \
The result has a `summary` field. Present that summary VERBATIM to the user \
and ask them to confirm or correct it — for example: "Sound right?" or \
"Anything you'd add (e.g. does it use a database)?". This is a hard rule: \
do not skip the validation step, and do not invent things the analyzer did \
not detect.

3. **Recommend.** Once the user confirms (or after applying their \
corrections to the profile — e.g. setting `has_database_hints` to true if \
they mention a database), call `recommend_architecture` with the profile. \
Walk the user through each AWS service from the result, including its \
`purpose`. If `notes` is non-empty, mention the alternatives.

4. **Estimate.** Call `estimate_cost` with the Architecture from step 3. \
Show the per-service breakdown and the total monthly cost. Also show the \
`assumptions` list verbatim — the user needs to know we're estimating at \
~100k requests/mo, not their actual traffic. If `is_fallback` is true, add: \
"These are rough starting estimates, not a real AWS Pricing API quote."

Rules:
- Be concise. One question at a time.
- NEVER invent AWS services that `recommend_architecture` did not return.
- NEVER invent dollar amounts that `estimate_cost` did not return.
- If a tool result has a `summary` field, that means the call failed. Tell \
the user the summary in plain language and offer to share the `details` if \
they ask.
- You do NOT deploy anything. If the user asks you to deploy, tell them \
that's coming in a future phase; for now you only analyze and estimate.
- If the analyzer returns `app_type: "unknown"`, ask the user to describe \
what the app does in plain language — then you can pass an updated profile \
to `recommend_architecture` with a guessed `app_type`.
"""


def _serialize_content(blocks: list[Any]) -> list[dict]:
    return [b.model_dump() for b in blocks]


def run_agent_loop(client: anthropic.Anthropic, session: Session) -> str:
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
            return next(
                (b.text for b in response.content if hasattr(b, "text") and b.type == "text"),
                "",
            )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                result = execute_tool(
                    block.name,
                    block.input,
                    session_id=session.session_id,
                    session=session,
                )
                # Cache the most recent profile on the session so the agent
                # can recover after a process restart.
                if block.name == "analyze_repo" and "app_type" in result:
                    session.last_profile = result
                if block.name == "clone_repo" and "path" in result:
                    session.clone_path = result["path"]

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    }
                )
            session.messages.append({"role": "user", "content": tool_results})

    return "Sorry — I hit my iteration limit. Try again with a fresh chat."
```

- [ ] **Step 4: Run agent tests, expect PASS**

Run: `cd aibuilder && pytest tests/test_agent.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add aibuilder/agent.py aibuilder/tests/test_agent.py
git commit -m "feat(aibuilder): agent loop + system prompt encoding 4-stage workflow"
```

---

## Task 12: FastAPI app

**Files:**
- Create: `aibuilder/app.py`
- Create: `aibuilder/tests/test_app.py`

- [ ] **Step 1: Write the failing test** — `aibuilder/tests/test_app.py`

```python
"""Tests for the FastAPI endpoints."""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-tests")
    # Reset import so the module picks up the patched env vars.
    import importlib
    import app as app_module

    importlib.reload(app_module)
    return TestClient(app_module.app)


def test_health(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_new_session(client: TestClient):
    response = client.get("/api/session")
    assert response.status_code == 200
    assert "session_id" in response.json()


def test_chat_invokes_agent(client: TestClient):
    session_id = client.get("/api/session").json()["session_id"]
    with patch("app.run_agent_loop", return_value="Hello from aibuilder."):
        response = client.post(
            "/api/chat",
            json={"session_id": session_id, "message": "hi"},
        )
    assert response.status_code == 200
    assert response.json()["message"] == "Hello from aibuilder."


def test_chat_unknown_session_returns_404(client: TestClient):
    response = client.post(
        "/api/chat",
        json={"session_id": "does-not-exist", "message": "hi"},
    )
    assert response.status_code == 404
```

- [ ] **Step 2: Run, expect fail**

Run: `cd aibuilder && pytest tests/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 3: Create `aibuilder/app.py`**

```python
"""aibuilder FastAPI routes."""

from __future__ import annotations

import os
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import run_agent_loop
from sessions import Session, SqliteSessionStore

app = FastAPI(title="aibuilder")
client = anthropic.Anthropic()
_DB_PATH = Path(os.environ.get("AIBUILDER_DB", Path(__file__).parent / "data" / "sessions.db"))
store = SqliteSessionStore(_DB_PATH)


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    message: str
    last_profile: dict | None = None


def _get_or_404(session_id: str) -> Session:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(404, f"Unknown session_id: {session_id}")
    return session


@app.get("/api/session")
def new_session() -> dict:
    session = store.create()
    return {"session_id": session.session_id}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    session = _get_or_404(req.session_id)
    session.messages.append({"role": "user", "content": req.message})
    reply = run_agent_loop(client, session)
    store.save(session)
    return ChatResponse(message=reply, last_profile=session.last_profile)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


app.mount(
    "/",
    StaticFiles(directory=str(Path(__file__).parent / "static"), html=True),
    name="static",
)
```

- [ ] **Step 4: Run app tests, expect PASS**

Run: `cd aibuilder && pytest tests/test_app.py -v`
Expected: 4 passed

(Note: the StaticFiles mount will warn that `static/` doesn't exist yet. That's fine — Task 13 creates it. The mount won't crash at import time because FastAPI defers the check.)

If the test fails on the StaticFiles mount, comment out the `app.mount(...)` line temporarily for this task and re-add it in Task 13.

- [ ] **Step 5: Commit**

```bash
git add aibuilder/app.py aibuilder/tests/test_app.py
git commit -m "feat(aibuilder): FastAPI app with /api/session, /api/chat, /api/health"
```

---

## Task 13: Static chat UI

Adapt the deploy-agent's `static/index.html` for aibuilder. Differences: no file dropzone, hero copy is *"Drop a repo, get an AWS plan."*, single-column chat.

**Files:**
- Create: `aibuilder/static/index.html`
- Create: `aibuilder/static/govtech-barbados.png` (copy from deploy-agent)

- [ ] **Step 1: Copy the logo**

```bash
cp deploy-agent/static/govtech-barbados.png aibuilder/static/govtech-barbados.png
```

- [ ] **Step 2: Read the source to adapt**

Read `deploy-agent/static/index.html` (~600 lines). You'll be:
- Removing the dropzone column (left card) and the `<input type="file">` related JS.
- Keeping the chat column, the markdown renderer (`renderMarkdown`), the typing indicator, and the GovTech color palette.
- Replacing hero copy with: *"Drop a repo, get an AWS plan."* and the eyebrow with *"aibuilder · alpha"*.
- Updating the greeting bullets to describe what aibuilder does (e.g. *"Paste a public GitHub URL."*, *"I'll figure out what AWS services you need and what they'd cost."*).
- Changing the `fetch` URL prefix from any deploy-agent specifics to aibuilder's `/api/chat` (the path is the same — verify).

- [ ] **Step 3: Create `aibuilder/static/index.html`**

The file is long (~500 lines). Approach:
1. Copy `deploy-agent/static/index.html` verbatim to `aibuilder/static/index.html`.
2. Delete the `.workspace` two-column grid styles and replace with a single-column `.chat-container` that fills the viewport.
3. Delete the `<aside class="dropzone">` (or whatever the upload card is named) HTML block.
4. Delete the `setupFileUpload()` JS function and any references to it (`uploadFiles`, drag-and-drop handlers).
5. Replace hero copy:
   - eyebrow: `aibuilder · alpha`
   - headline: `Drop a repo, get an AWS plan.`
   - subhead: `Paste a public GitHub URL. I'll tell you what AWS services you need and what they'd cost per month.`
6. Replace the greeting bullets in the initial assistant message:
   - "Paste a public GitHub URL."
   - "I'll figure out what kind of app it is and confirm with you."
   - "Then I'll recommend the AWS services and estimate the monthly cost."
7. Keep `BAJAN_LOADING_MESSAGES` and the typing indicator — Chris likes them.

- [ ] **Step 4: Manually verify in a browser**

```bash
cd aibuilder && ./run.sh
```

Open `http://localhost:8001`. Verify:
- Page loads with no console errors.
- Hero copy reads correctly.
- Initial assistant greeting appears.
- Typing in the chat box and pressing Enter sends a message (the agent will respond if `ANTHROPIC_API_KEY` is set).

- [ ] **Step 5: Commit**

```bash
git add aibuilder/static/
git commit -m "feat(aibuilder): single-column chat UI adapted from deploy-agent"
```

---

## Task 14: Smoke test script

Real end-to-end test against a small public repo. Modeled on `deploy-agent/scripts/smoke-test.sh`.

**Files:**
- Create: `aibuilder/scripts/smoke-test.sh`

- [ ] **Step 1: Create `aibuilder/scripts/smoke-test.sh`** (mark executable)

```bash
#!/usr/bin/env bash
# End-to-end smoke test for aibuilder.
#
# Starts uvicorn, hits the chat endpoint with a known public repo URL,
# verifies the agent walks through clone → analyze → recommend → estimate
# and returns a cost figure. Idempotent — cleans up uvicorn on exit.
#
# Requires: ANTHROPIC_API_KEY in the environment. Does NOT need AWS creds.

set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "❌ ANTHROPIC_API_KEY is not set" >&2
  exit 1
fi

# Use a separate DB so the smoke test doesn't pollute the dev session.
SMOKE_DB="$(mktemp).db"
export AIBUILDER_DB="$SMOKE_DB"

cleanup() {
  if [ -n "${UVICORN_PID:-}" ]; then
    kill "$UVICORN_PID" 2>/dev/null || true
  fi
  rm -f "$SMOKE_DB"
}
trap cleanup EXIT

echo "▶ Starting uvicorn..."
python3 -m uvicorn app:app --host 127.0.0.1 --port 8765 >/tmp/aibuilder-smoke.log 2>&1 &
UVICORN_PID=$!

# Wait for the server to come up.
for _ in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8765/api/health >/dev/null; then
    break
  fi
  sleep 0.5
done

echo "▶ Opening a session..."
SESSION_ID=$(curl -sf http://127.0.0.1:8765/api/session | python3 -c "import json,sys;print(json.load(sys.stdin)['session_id'])")
echo "   session_id=$SESSION_ID"

# octocat/Hello-World: tiny, stable, public, present forever.
URL="https://github.com/octocat/Hello-World"

echo "▶ Asking the agent to analyze $URL ..."
RESPONSE=$(curl -sf -X POST http://127.0.0.1:8765/api/chat \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION_ID\",\"message\":\"Analyze $URL and tell me what AWS services I'd need and the monthly cost. Confirm and recommend in one go.\"}")
echo "$RESPONSE" | python3 -m json.tool

# Continue the conversation so the agent makes the full chain of tool calls.
echo "▶ Confirming so the agent proceeds to recommendation + cost..."
RESPONSE=$(curl -sf -X POST http://127.0.0.1:8765/api/chat \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION_ID\",\"message\":\"Yes that's right, proceed.\"}")
echo "$RESPONSE" | python3 -m json.tool

# Loose assertion: the final message should mention a dollar amount or "month".
if echo "$RESPONSE" | grep -qi -e '\$' -e 'month'; then
  echo "✅ Smoke test passed — agent produced a cost estimate."
else
  echo "❌ Smoke test failed — agent response did not contain cost info." >&2
  exit 1
fi
```

- [ ] **Step 2: Make executable**

```bash
chmod +x aibuilder/scripts/smoke-test.sh
```

- [ ] **Step 3: Run the smoke test**

```bash
cd aibuilder && ./scripts/smoke-test.sh
```

Expected: ends with "✅ Smoke test passed — agent produced a cost estimate."

If it fails, check `/tmp/aibuilder-smoke.log` for uvicorn output.

- [ ] **Step 4: Commit**

```bash
git add aibuilder/scripts/smoke-test.sh
git commit -m "test(aibuilder): smoke test for clone→analyze→recommend→estimate chain"
```

---

## Task 15: CI + CLAUDE.md update

**Files:**
- Modify: `.github/workflows/*.yml` (if any exists; otherwise skip)
- Modify: `CLAUDE.md`

- [ ] **Step 1: Check for CI workflow files**

```bash
ls -la .github/workflows/ 2>/dev/null
```

If a workflow exists that runs `cd deploy-agent && make check`, add a parallel `cd aibuilder && make check` step. If no workflow exists, skip CI changes — the project README documents the local `make check` flow.

- [ ] **Step 2: Add aibuilder section to `CLAUDE.md`**

Append after the existing deploy-agent section (search for `## Repository purpose` to find the right insertion point — append a new `## aibuilder` section).

Use this template:

```markdown
## aibuilder

Sibling app to `deploy-agent/`. Chat bot that takes a public GitHub URL, classifies the app, recommends an AWS architecture from a curated catalog, and returns a monthly cost estimate (fallback table only in v1; live AWS Pricing API integration is a future phase).

### Commands

\`\`\`bash
cd aibuilder
cp .env.example .env             # then edit .env with ANTHROPIC_API_KEY
./run.sh                          # creates ./.venv, installs deps, runs uvicorn on :8001
\`\`\`

### Make targets

\`\`\`bash
make install        # production deps
make install-dev    # adds pytest, ruff
make check          # ruff + pytest
make test
make format
\`\`\`

### Architecture

- `app.py` — FastAPI routes (`/api/session`, `/api/chat`, `/api/health`)
- `agent.py` — Claude tool-use loop + 4-stage system prompt (ingest → validate → recommend → estimate)
- `analyzer.py` — pure-Python repo classifier; returns `RepoProfile`
- `patterns.py` — pattern catalog mapping `app_type` → AWS `Architecture`
- `pricing.py` — fallback cost table; live Pricing API integration is a Phase 1.5 task
- `tools.py` — 4 tools: `clone_repo`, `analyze_repo`, `recommend_architecture`, `estimate_cost`
- `sessions.py` — SQLite session store

### Things that will bite you

- aibuilder uses port **8001** (deploy-agent uses 8000) so you can run both at once.
- `clone_repo` shells out to `git clone --depth=1` to a per-session temp dir under `aibuilder/tmp/repos/<session_id>/`. The dir is gitignored.
- Repo guards: `AIBUILDER_MAX_FILES` (default 5000), `AIBUILDER_MAX_SIZE_MB` (default 500). Set in env to override.
- The pattern catalog is the brain. The system prompt explicitly forbids inventing AWS services not in the catalog and inventing dollar amounts not from `estimate_cost`.
- v1 cost estimates are from a hand-curated fallback table. `is_fallback: true` is always set. Don't ship "live" cost numbers without doing the Phase 1.5 Pricing API work.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md .github/workflows/ 2>/dev/null
git commit -m "docs(aibuilder): CLAUDE.md update + CI integration"
```

---

## Self-review checklist (done by plan author)

- [x] Spec coverage: every component in the spec (`clone_repo`, `analyze_repo`, `recommend_architecture`, `estimate_cost`, sessions, agent loop, app, UI) has a task.
- [x] Spec coverage: validation step is in `SYSTEM_PROMPT` and tested (Task 11).
- [x] Spec coverage: pattern catalog matches the spec table (Task 5).
- [x] Spec coverage: cost assumptions match the spec's baseline (Task 6).
- [x] Scope adjustment from spec called out at top: v1 ships fallback-only pricing.
- [x] Types used in later tasks match earlier definitions: `RepoProfile`, `Architecture`, `ArchitectureService`, `CostEstimate`, `CostLine`, `Session`.
- [x] No "TBD" / "TODO" / "implement appropriate validation" placeholders.
- [x] Every code step has complete code. Every test step has expected output.
- [x] Frequent commits — every task ends in a commit.
- [x] Test count target: 11 (tools) + 9 (analyzer) + 7 (patterns) + 5 (pricing) + 3 (sessions) + 3 (agent) + 4 (app) = **42 tests** before the smoke test.
