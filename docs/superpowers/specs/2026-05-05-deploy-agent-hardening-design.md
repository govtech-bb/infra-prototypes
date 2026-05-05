# Deploy Agent Hardening — Design

**Date:** 2026-05-05
**Scope:** Sub-project A of "demoable to a stranger." Hardens the existing local prototype into a real public repo with persistence, tests, CI, and documentation. **Does not** include hosting the agent itself for strangers — that's sub-project B.

## Status

✅ **Shipped 2026-05-05** at https://github.com/christophercorbin/infra-prototypes. CI green on initial push. The real-AWS smoke test (`scripts/smoke-test.sh`) is left as a manual run — defer until shell has `ANTHROPIC_API_KEY` + AWS creds + uvicorn installed.

## Goals

Turn `INFRA prototypes/` into a public GitHub repo that:

- Has a working git history and a README a stranger can follow.
- Has a real session store so chat history survives server restarts.
- Has automated tests that catch logic bugs without requiring AWS.
- Has a manual smoke test that catches AWS-shaped bugs before release.
- Has CI that lints, tests, and validates infra on every push.
- Fixes the known upload path bug so nested-asset sites actually work.
- Surfaces friendlier tool errors in the chat UI.

## Non-goals (explicit)

- Auth, rate limiting, multi-tenant deployment of the agent itself.
- Remote tofu state backend (kept local; migration comment stays in `backend.tf`).
- New tofu stacks beyond `static-website`.
- Streaming responses, concurrent-session safety.
- Cache policy modernization for the CloudFront module (deferred polish).
- `infra/README.md` (deferred polish).

These belong to later sub-projects.

## Architecture

The big architecture is unchanged: synchronous Claude agent loop, predefined OpenTofu stacks, workspace-per-deployment, FastAPI + static UI. The light refactor splits `app.py` so each module has one job.

```
deploy-agent/
  app.py                # FastAPI route handlers only
  agent.py              # run_agent_loop, system prompt, MAX_AGENT_ITERATIONS
  sessions.py           # SQLite-backed Session + SessionStore
  tools.py              # tool defs + impls; path bug fixed; _classify_error
  static/index.html     # unchanged
  pyproject.toml        # ruff config + project metadata
  requirements.txt      # pinned versions
  run.sh                # unchanged
  Makefile              # NEW — `make check`, `make destroy-all`
  data/                 # NEW — gitignored, holds sessions.db
  tests/
    conftest.py
    test_app.py
    test_agent.py
    test_sessions.py
    test_tools.py

infra/                  # unchanged
examples/
  sample-site/          # NEW — drag-in fixture + smoke-test target
    index.html
    style.css
    assets/logo.svg
scripts/
  smoke-test.sh         # NEW — opt-in real-AWS deploy/destroy cycle
  destroy_all.py        # NEW — reads sessions.db, tears down every deployment

.github/workflows/ci.yml
docs/superpowers/specs/
README.md
.gitignore
CLAUDE.md               # already exists — updated in step 10
```

`agent.py` owns the system prompt and the tool-use loop. `app.py` becomes a thin FastAPI shim. `sessions.py` is the only thing that touches SQLite. `data/` and `*.tfstate` are gitignored. `examples/sample-site/` doubles as smoke-test fixture and README onboarding.

## Component design

### `sessions.py`

Single SQLite table at `data/sessions.db`:

```sql
CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,
  messages     TEXT NOT NULL DEFAULT '[]',  -- JSON list of {role, content}
  files        TEXT NOT NULL DEFAULT '[]',  -- JSON list of relative filenames
  upload_dir   TEXT,
  deployment   TEXT,                         -- JSON dict or NULL after first deploy
  project_name TEXT,                         -- denormalized for destroy_all.py
  env          TEXT,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

`project_name` and `env` are denormalized from `deployment` so `scripts/destroy_all.py` can `SELECT project_name, env FROM sessions WHERE deployment IS NOT NULL` without parsing JSON.

API:

```python
@dataclass
class Session:
    session_id: str
    messages: list[dict]
    files: list[str]
    upload_dir: str | None
    deployment: dict | None

class SessionStore:
    def __init__(self, db_path: Path) -> None: ...
    def create(self) -> Session: ...           # generates UUID, inserts, returns
    def get(self, session_id: str) -> Session | None: ...
    def save(self, session: Session) -> None: ...  # upsert; bumps updated_at
```

`project_name` and `env` are not fields on the `Session` dataclass — they are derived columns. `SessionStore.save` extracts them from `session.deployment` (when present) and writes them alongside the JSON blob. This keeps the in-memory model clean while letting `destroy_all.py` query by SQL.

No migrations. Schema is created with `CREATE TABLE IF NOT EXISTS` on first connection.

### `agent.py`

Houses the system prompt, the tool-use loop extracted from `app.py`, and:

```python
MAX_AGENT_ITERATIONS = 15  # was hardcoded in the loop; now discoverable + override-able in tests
```

**Message serialization fix.** Anthropic `ContentBlock` objects (`TextBlock`, `ToolUseBlock`) don't serialize to JSON natively. When appending the assistant turn to session history, convert via `[b.model_dump() for b in response.content]`. The Anthropic API accepts plain dicts in the `messages.content` field on subsequent calls, so the round-trip is lossless. One helper `_serialize_content(blocks)` in `agent.py` is called at the single append site.

System-prompt update: when a tool returns `{"summary": ..., "details": ...}`, the agent reports `summary` to the user and offers `details` if asked.

### `tools.py`

Two changes:

1. **`_classify_error(stderr: str) -> dict`** — pattern-matches known failure modes:

   ```python
   PATTERNS = [
       ("AccessDenied",            "AWS credentials are missing or lack permission. Check AWS_PROFILE / IAM."),
       ("NoCredentialProviders",   "No AWS credentials found. Set AWS_PROFILE or AWS_ACCESS_KEY_ID."),
       ("BucketAlreadyOwnedByYou", "A bucket with this name already exists in your account. Pick a different project_name."),
       ("Error: error configuring", "AWS configuration error — check your region and credentials."),
   ]
   ```

   Returns `{"summary": "...", "details": stderr[-2000:]}`. Unknown errors fall through to a generic summary `"Deployment failed — see details."` with the raw tail attached. `tofu workspace ... already exists` is benign and silently swallowed (existing behavior preserved).

2. **No changes to tool surface area.** `deploy_infrastructure` and `upload_files` keep their schemas. Internal error returns become structured dicts; happy-path returns are unchanged.

### `app.py`

Becomes a thin FastAPI shim:

- Module-scoped `SessionStore` initialized at startup using `data/sessions.db` (path overridable via env var for tests).
- Route handlers (`/api/session`, `/api/upload/{session_id}`, `/api/chat`, `/api/health`) delegate to `agent.run_agent_loop` and `SessionStore`.
- **Upload path bug fix** (currently `app.py:152`):

  ```python
  filename = f.filename or ""
  parts = Path(filename).parts
  if not parts or any(p in ("..", "") or p.startswith("/") for p in parts):
      raise HTTPException(400, f"Invalid filename: {filename!r}")
  safe_path = Path(upload_dir) / filename
  safe_path.parent.mkdir(parents=True, exist_ok=True)
  ```

  Preserves nested paths. Blocks `..` traversal and absolute paths.

### `scripts/smoke-test.sh`

Opt-in real-AWS deploy/destroy cycle. Safe to re-run (each run uses a unique `PROJECT` name so there's no collision with prior runs):

```bash
#!/usr/bin/env bash
# Full deploy → upload → verify → destroy cycle against a real AWS account.
# Requires: ANTHROPIC_API_KEY, AWS credentials. Costs ~$0.01.
# Cleanup runs even on failure (trap EXIT).

set -euo pipefail
PROJECT="smoke-$(date +%s)"
ENV="smoke"
trap 'cd infra && make destroy PROJECT="$PROJECT" ENV="$ENV" || true' EXIT

# 1. Start the agent server in background.
# 2. Hit /api/session, /api/upload (with examples/sample-site/), /api/chat
#    with a single fully-specified message:
#      "Deploy this site for Smoke Test (smoke@example.com), not a single-page app.
#       Use project_name=$PROJECT and env=$ENV."
# 3. Poll /api/chat replies until session.deployment is populated; capture site_url.
# 4. curl $site_url; assert HTTP 200 and a known string from the sample site is in the body.
```

Design intent: the scripted message provides every required field upfront so the agent has no reason to ask follow-ups. If the agent still asks, the smoke test fails — that is correct behavior; it caught a real bug.

### `scripts/destroy_all.py`

```python
# Reads data/sessions.db; for each session with deployment IS NOT NULL,
# runs `tofu workspace select <project>-<env>` then `tofu destroy -auto-approve`
# with -var=project_name=... -var=env=... pulled from the row.
# Skips and logs sessions whose workspace no longer exists.
```

Reads from SQLite rather than parsing workspace names, so `project_name` containing hyphens stays correct.

### `examples/sample-site/`

Minimal three-file static site:

- `index.html` — landing page with a stable marker string the smoke test can grep for (e.g. `"deployed via INFRA Deploy Agent"`).
- `style.css` — basic styling.
- `assets/logo.svg` — tiny logo at a nested path; its presence at `/assets/logo.svg` post-upload is what proves the path-stripping bug is fixed.

### `Makefile` (new, in `deploy-agent/`)

```make
check: lint test infra-validate
lint:
	ruff check . && ruff format --check .
test:
	pytest tests/
infra-validate:
	cd ../infra/stacks/static-website && tofu fmt -recursive -check && tofu init -backend=false && tofu validate
destroy-all:
	python scripts/destroy_all.py
```

## Testing

`deploy-agent/tests/` with one file per module under test.

| File                | Coverage |
|---------------------|----------|
| `test_sessions.py`  | round-trip create/get/save against `tmp_path` SQLite; mixed text+tool-use blocks serialize cleanly; denormalized `project_name`/`env` populated when `deployment` is set; `get` of unknown id returns `None`. |
| `test_tools.py`     | `_classify_error` matches each pattern + falls through; `deploy_infrastructure` happy path with mocked `subprocess.run` (asserts CLI args including all `-var=` flags); failure paths (init fails, apply fails, timeout) return structured `{summary, details}`; `upload_files` with `moto` covers nested-key upload, mime guessing, invalidation call. |
| `test_agent.py`     | happy path (text + end_turn); one tool round-trip caches deployment on session; iteration cap returns the cap-hit message; `ContentBlock` objects converted to dicts before save. |
| `test_app.py`       | upload-path: nested preserved, `..` rejected (400), absolute rejected (400), normal flat works. |

All tests run with no AWS credentials, no Anthropic key, no `tofu` binary required.

## Error handling

- **Tool failures** → structured `{summary, details}` returned to the agent → agent reports `summary` to user.
- **HTTP 400** for invalid uploads (path traversal, empty filename).
- **`MAX_AGENT_ITERATIONS` exhausted** → returns "The deployment agent reached its iteration limit. Please try again." (existing message, unchanged).
- **SQLite errors** → propagate as `HTTPException(500)`. We don't try to recover from disk-corruption-class problems; the user sees an error and we read the server log.
- **Smoke test failures** → trap-driven cleanup ensures no orphaned AWS resources, then exits non-zero with the failed step named.

## CI (`.github/workflows/ci.yml`)

Runs on push and PR to `main`. Single job:

```yaml
- ruff check . && ruff format --check .
- pytest deploy-agent/tests/
- cd infra && tofu fmt -recursive -check
- cd infra/stacks/static-website && tofu init -backend=false && tofu validate
```

`-backend=false` skips the (commented-out) backend so CI doesn't need AWS creds. Total runtime target: under 60 seconds.

## README outline

1. One-paragraph pitch.
2. Demo (animated GIF placeholder; recorded in sub-project B).
3. Architecture (one paragraph + a small diagram).
4. Quickstart (≤5 commands).
5. What it costs (~$0.50/month idle CloudFront; how to destroy).
6. Verifying it works (`./scripts/smoke-test.sh`).
7. Project layout (tree).
8. Limitations (local state, single-user, hardcoded to static-website stack; pointer to sub-project B).
9. Contributing (`make check`).

## `.gitignore`

```
# Python
__pycache__/
*.pyc
.venv/
.pytest_cache/
.ruff_cache/

# OpenTofu / Terraform
.terraform/
.terraform.lock.hcl
*.tfstate
*.tfstate.*
*.tfvars

# Agent runtime
deploy-agent/data/
/tmp/deploy-sessions/

# OS
.DS_Store
```

## Build sequence

Each step is a small, independently-working commit. After step 2, the app runs identically to today. After step 3, sessions survive restart. After step 7, the smoke test passes against real AWS. After step 9, CI is green. After step 11, the repo is public.

1. `git init`; add `.gitignore` and a README skeleton; commit existing code as the baseline.
2. Light refactor: extract `agent.py` and `sessions.py` (in-memory `SessionStore` stand-in, no behavior change). Sanity-check the chat still works end-to-end.
3. Replace in-memory `SessionStore` with the SQLite implementation. Verify sessions survive a server restart.
4. Apply the upload path bug fix in `app.py`; add `test_app.py` cases for nested/traversal/absolute.
5. Add `_classify_error` in `tools.py`; update the system prompt in `agent.py`; add `test_tools.py` cases.
6. Extract `MAX_AGENT_ITERATIONS` constant in `agent.py`.
7. Add `examples/sample-site/`, `scripts/smoke-test.sh`, `scripts/destroy_all.py`. Run smoke test against real AWS once.
8. Pin `requirements.txt` to exact versions; add `pyproject.toml` with ruff config; add the `Makefile` with `check` and `destroy-all` targets.
9. Add `.github/workflows/ci.yml`.
10. README final pass; update CLAUDE.md to reflect the new module split.
11. Create the GitHub repo and push.

## Open questions

None blocking. Two deferred polish items noted as out-of-scope:

- CloudFront `forwarded_values` → managed cache policy migration.
- `infra/README.md` for standalone-stack users.

Both are one-commit additions easy to land later.
