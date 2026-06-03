# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

Three sibling prototypes:

- `infra/` — OpenTofu (Terraform-compatible) stack that provisions an S3 + CloudFront static-site setup. Usable standalone via `make`.
- `deploy-agent/` — FastAPI chat UI; Claude (`claude-opus-4-6`) is given four tools (`deploy_infrastructure`, `upload_files`, `list_deployments`, `destroy_infrastructure`) that shell out to `tofu` and `boto3` to drive the `infra/` stack.
- `aibuilder/` — FastAPI chat UI; Claude is given four tools (`clone_repo`, `analyze_repo`, `recommend_architecture`, `estimate_cost`) that take a public GitHub URL, classify the app, recommend an AWS architecture from a curated catalog, and return a monthly cost estimate. v1 ships fallback-only pricing (live Pricing API is a follow-up). No deploys — analysis and estimate only.

All three pieces are siblings, not nested. `deploy-agent/tools.py` resolves `INFRA_DIR` as `../infra` relative to itself. `aibuilder/` has no `infra/` coupling — its tools clone external GitHub repos into `aibuilder/tmp/repos/<session_id>/`.

Public repo: <https://github.com/christophercorbin/infra-prototypes>. CI runs ruff + pytest + tofu validate on every push/PR.

## Commands

### Deploy agent (chat UI)

```bash
cd deploy-agent
cp .env.example .env             # then edit .env with ANTHROPIC_API_KEY + AWS_PROFILE
./run.sh                          # creates ./.venv, installs deps, runs uvicorn on :8000
```

`./run.sh` auto-loads `deploy-agent/.env` if present (`.env` is gitignored; `.env.example` is committed). It gates on `ANTHROPIC_API_KEY` and warns on missing AWS creds. Open <http://localhost:8000>.

### Make targets (`deploy-agent/Makefile`)

```bash
make install        # production deps
make install-dev    # adds pytest, moto, ruff
make check          # ruff + pytest + tofu fmt -check + tofu validate
make test           # just pytest
make format         # ruff check --fix && ruff format
make destroy-list   # print every active deployment recorded in sessions.db
make destroy-all    # tear down every recorded deployment via tofu destroy
```

### Infra stack (manual / standalone, `infra/Makefile`)

All targets accept `PROJECT=` and `ENV=` overrides; `STACK` defaults to `static-website`:

```bash
make init                                       # tofu init in stacks/$(STACK)
make plan    PROJECT=myapp ENV=chris-test
make deploy  PROJECT=myapp ENV=chris-test       # tofu apply -auto-approve, prints site_url
make upload  PROJECT=myapp ENV=chris-test       # syncs ./dist to S3 + invalidates CF
make destroy PROJECT=myapp ENV=chris-test
```

The `upload` target reads `bucket_name` and `cloudfront_distribution_id` from `tofu output -raw` of the currently selected workspace, so run it from the same shell context as the deploy.

### Smoke test (real AWS, ~$0.01)

`./scripts/smoke-test.sh` runs a full deploy → upload → assert-content → destroy cycle. Requires `ANTHROPIC_API_KEY` + AWS creds + `uvicorn` on PATH. Cleanup runs even on failure (trap EXIT).

## Architecture

### Agent loop (`deploy-agent/agent.py`)

`run_agent_loop` is a hand-rolled tool-use loop with `MAX_AGENT_ITERATIONS = 15` (overridable in tests). Each turn:

1. Calls `client.messages.create` with the cumulative `session.messages`.
2. Appends the assistant's `content` blocks to history (serialized via `_serialize_content` so `ContentBlock` objects round-trip through SQLite as plain dicts).
3. On `stop_reason == "end_turn"`, returns the first text block.
4. On `stop_reason == "tool_use"`, executes every `tool_use` block via `tools.execute_tool`, packages results as a single `user` message of `tool_result` blocks, and loops.

Sessions are persisted in a SQLite file at `deploy-agent/data/sessions.db` via `SqliteSessionStore` in `sessions.py`. Single-table schema with `project_name` and `env` as denormalized columns (so `scripts/destroy_all.py` and `_read_active_deployments` can `SELECT` without parsing JSON). The schema also has `last_injected_file_count` for the file re-injection logic. Override the path with `DEPLOY_AGENT_DB` env var (used by tests).

When files are uploaded via `/api/upload/{session_id}`, `app.py`'s chat handler tracks `session.last_injected_file_count` and re-injects only newly uploaded files. First batch becomes `[Uploaded files: …]` (legacy format the system prompt was trained on); subsequent batches become `[Newly uploaded: …]`.

### Tool implementations (`deploy-agent/tools.py`)

Four tools, all synchronous:

- **`deploy_infrastructure`** runs preflight on uploaded files (rejects empty / source-only uploads, auto-detects `index.html` or a single non-index HTML), enforces rate limits (currently a placeholder — see "Things that will bite you"), then `tofu init` → `tofu workspace new/select <project>-<env>` → `tofu apply` with `-var=` flags → parses `tofu output -json`. Returns success dict including `bucket_name`, `site_url`, `cloudfront_distribution_id`, `project_name`, `env`. Workspaces isolate tofu state per deployment.

- **`upload_files`** uses `boto3` to `upload_file` everything under `session.upload_dir`, guesses MIME types via `mimetypes`, then calls `cloudfront.create_invalidation` with `Paths: ["/*"]`. Same tool used for first deploys AND for updates (re-upload to existing bucket).

- **`list_deployments`** (read-only) reads `sessions.db`, intersects with `tofu workspace list` output (so destroyed-but-still-recorded sessions don't appear), and returns each active deployment with `project_name`, `env`, `site_title`, `owner_name`, `site_url`, `bucket_name`, `cloudfront_distribution_id`, `updated_at`.

- **`destroy_infrastructure`** is two-phase. Called with `confirm=False` (default) returns `{preview: True, message, deployment}` — no `summary` field, agent surfaces `message` verbatim. Called with `confirm=True` runs `tofu init` → `tofu workspace select` → `_empty_bucket` (boto3 paginated delete_objects so the non-empty-bucket failure mode is gone) → `tofu destroy`. Workspace-already-gone is treated as idempotent success (only the canonical "does not exist" stderr — other failures classify as real errors). On success, `_maybe_clear_session_deployment` clears `session.deployment` if it matches.

Tool failures return `{"summary": "...", "details": "..."}` dicts (never raised). The system prompt instructs the agent to report `summary` to the user and offer `details` if asked. `_classify_error` maps tofu/AWS stderr (case-insensitive) to friendly summaries; pattern order matters — bucket-collision is checked before AccessDenied.

### Preflight (`tools._preflight_uploads`)

Inspects uploaded files before `tofu init`. First match wins:

1. **Empty / missing dir** → "No files uploaded yet — drag a folder into the chat first."
2. **`index (N).html` browser-duplicate download** → "Rename it to `index.html` and upload again." (Regex: `^index\s*\(\d+\)\.html?$`, case-insensitive.)
3. **Source code (`.jsx|.tsx|.ts|.vue|.svelte|.scss|.sass|.less` by suffix) with no HTML** → "Run 'npm run build' and upload the output folder." Match is on `Path.suffix`, not name, so `notes-on-tsx-migration.txt` doesn't false-positive.
4. **`index.html` present** → pass through, default `index_document="index.html"`.
5. **Single non-index HTML file** → auto-select as `index_document` (preserves original case).
6. **Multiple HTML files, no `index.html`** → ask the user to pick.

`deploy_infrastructure` accepts an optional `index_document` parameter that overrides auto-detection. Threaded through to tofu via `-var=index_document=<value>` (only when non-default — the stack already defaults to `index.html`).

### Terraform stack (`infra/stacks/static-website/`)

Composes two modules: `modules/s3-static-site` (private bucket + public-access-block, optional versioning, **`force_destroy = true`**) and `modules/cloudfront` (distribution + OAC, SPA mode toggles `custom_error_response` for 403/404 → `index.html`).

**Watch this:** the S3 bucket policy granting CloudFront read access lives in the **stack**, not the s3 module (`stacks/static-website/main.tf:59-82`). This is deliberate — putting it in the s3 module would create a circular dependency (S3 needs CF's ARN, CF needs S3's regional domain). When adding new resources, keep this split in mind.

The `viewer_certificate` block uses flat attributes (`acm_certificate_arn`, `ssl_support_method`, `minimum_protocol_version`) rather than a `dynamic "acm_certificate"` block — AWS provider 5.x removed the nested form.

Tags flow from `locals.common_tags` in the stack and are applied via the AWS provider's `default_tags`, so module resources inherit them automatically.

### State backend

`backend.tf` is intentionally commented out — prototypes use local state. The file contains a ready-to-uncomment S3 backend block for graduating to shared state. If you migrate, run `tofu init` to migrate state.

### Static UI (`deploy-agent/static/index.html`)

Single-file HTML+CSS+JS. GovTech Barbados aesthetic:

- Deep purple (`#3A3380`) top bar with "Official GovTech Barbados internal tool" framing + alpha banner
- Hero with the GovTech logo, eyebrow, "Ship a prototype to AWS in a minute." headline
- Two-column workspace: file dropzone (left card) + chat (right card)
- Chat bubbles render a focused subset of markdown (tables, headings, lists, bold, italic, inline code, code blocks, links) via `renderMarkdown` — DOM-based, no `innerHTML`, URL scheme validated against `^(https?://|mailto:|/|#)/`. LLM-generated content can't escape the renderer.
- Typing indicator cycles through Bajan-flavored loading messages (`BAJAN_LOADING_MESSAGES` array) every 2.4s. `setInterval` is cleared when the typing element is removed (patched `.remove()`).

Logo asset at `deploy-agent/static/govtech-barbados.png` is served at `/govtech-barbados.png`.

## Conventions

- **Default env** for agent-driven deploys is `proto` (per `SYSTEM_PROMPT` in `agent.py`); the standalone `Makefile` defaults to `dev`. Don't mix them — they create different workspaces.
- **Bucket naming** is `${project_name}-${env}-static`. `project_name` collisions across envs are fine but cross-env collisions are not.
- **`is_spa=true`** turns CloudFront 403/404s into 200s serving `index.html` — only enable for SPAs, otherwise it masks real 404s.
- `price_class` defaults to `PriceClass_100` (US/EU edges only) for prototype cost.
- The agent model is pinned to `claude-opus-4-6` in `agent.py` (look for the `model=` kwarg in `client.messages.create`). To change, edit there — no env override.
- **Communication preference for this project:** fast iteration with bundled commits, light brainstorming flow, "go" as the green-light trigger. Bajan-flavored humor is welcome.

## System-prompt workflows

The agent's system prompt (`agent.py:SYSTEM_PROMPT`) defines four flows:

1. **Deploy:** collect site_title / owner_name / owner_email / is_spa, summarize, call `deploy_infrastructure`, then `upload_files`, return live URL.
2. **Update:** identify deployment by name (use `session.deployment` for "this", else call `list_deployments`), grab `bucket_name` + `cloudfront_distribution_id` from the matched record, call `upload_files` directly. Don't call `deploy_infrastructure` — infra already exists.
3. **List:** call `list_deployments`, render results.
4. **Destroy:** call `destroy_infrastructure(confirm=false)` → relay `message` verbatim, ask user to confirm → call again with `confirm=true`. Never fuzzy-match; cite exact `project_name`+`env`.

## Things that will bite you

- The agent calls `tofu`, not `terraform` — make sure OpenTofu is installed and on PATH.
- `tofu workspace new` is run unconditionally and ignores its non-zero exit code (workspace-already-exists is expected). If you see weird state, check `tofu workspace list` in `infra/stacks/static-website/`.
- ACM certs for CloudFront **must** be in `us-east-1` regardless of `aws_region` — see `acm_certificate_arn` in `variables.tf:65`.
- File uploads preserve nested paths and reject `..` traversal / absolute paths / Windows backslashes with HTTP 400. `examples/sample-site/assets/logo.svg` is the regression fixture — if you break path preservation, the smoke test fails.
- `force_destroy = true` is set on the s3 bucket. Existing deployments don't have this in their state until they're re-applied; the agent's `_empty_bucket` helper covers the gap by emptying via boto3 before `tofu destroy`.
- Sessions persisted in SQLite have an `ALTER TABLE ADD COLUMN` migration in `SqliteSessionStore.__init__` (currently for `last_injected_file_count`). The migration is wrapped in `try/except sqlite3.OperationalError` so it's idempotent. If you add another column later, add another defensive ALTER.

## aibuilder

Sibling app to `deploy-agent/`. Chat bot that takes a public GitHub URL, classifies the app, recommends an AWS architecture from a curated catalog, and returns a monthly cost estimate. v1 ships with a fallback-table cost estimator — live AWS Pricing API integration is the first follow-up.

### Commands

```bash
cd aibuilder
cp .env.example .env             # then edit .env with ANTHROPIC_API_KEY
./run.sh                          # creates ./.venv, installs deps, runs uvicorn on :8001
```

`./run.sh` auto-loads `aibuilder/.env` if present (`.env` is gitignored; `.env.example` is committed). Gates on `ANTHROPIC_API_KEY`. Open <http://localhost:8001>.

### Make targets (`aibuilder/Makefile`)

```bash
make install        # production deps
make install-dev    # adds pytest, ruff
make check          # ruff check + ruff format --check + pytest
make test           # just pytest
make format         # ruff check --fix && ruff format
```

### Architecture

Same shape as deploy-agent (FastAPI + Claude tool-use loop + SQLite sessions + single-file static chat UI), with a different toolset and no upload path.

- **`app.py`** — Routes: `GET /api/session`, `POST /api/chat`, `GET /api/health`. The chat handler appends the user message, runs `agent.run_agent_loop`, saves the session, and returns `{message, last_profile}`. `static/` is mounted at `/`.
- **`agent.py`** — Hand-rolled tool-use loop (`run_agent_loop`, `MAX_AGENT_ITERATIONS = 15`). `SYSTEM_PROMPT` encodes the 4-stage workflow: ingest → validate → recommend → estimate. The prompt explicitly forbids inventing AWS services or dollar amounts.
- **`analyzer.py`** — Pure-Python `analyze_repo(path) → RepoProfile`. Walks the cloned tree, parses `package.json` / `requirements.txt` / `pyproject.toml` / `Dockerfile` / `.env*`, and classifies into one of 7 app types: `static_site`, `spa_with_api`, `node_api`, `python_api`, `dockerized_web`, `fullstack_with_db`, `worker`. Also builds a human-readable summary string the agent surfaces verbatim during the validation step.
- **`patterns.py`** — `recommend(profile) → Architecture` does a dict lookup against `_CATALOG` (the deterministic brain). Each `Architecture` has a list of `ArchitectureService` dataclasses with `aws_service`, `purpose`, `sizing`. Special case: `spa_with_api + has_database_hints` upgrades to `fullstack_with_db`.
- **`pricing.py`** — `estimate(architecture) → CostEstimate` looks each service up in `_FALLBACK_PRICES` (curated table) and sums. `CostEstimate` has `lines`, `total_monthly_usd`, `assumptions`, `is_fallback`. `is_fallback` is always `True` in v1.
- **`tools.py`** — Four tools registered in `TOOL_DEFINITIONS` and dispatched by `execute_tool(name, args, *, session_id, session)`:
  - `clone_repo(github_url)` — validates URL via `_GITHUB_URL_RE`, shallow-clones to `aibuilder/tmp/repos/<session_id>/<repo>/`, enforces size + file-count guards. Rejects path-traversal segments (`.`, `..`) and double-checks `target.resolve().relative_to(session_root)` before any `rmtree`.
  - `analyze_repo(path)` — thin wrapper around `analyzer.analyze_repo`, returns a JSON-shaped `RepoProfile.to_dict()`.
  - `recommend_architecture(profile: dict)` — reconstructs the dataclass from the dict, calls `patterns.recommend`, returns `.to_dict()`.
  - `estimate_cost(architecture: dict)` — same pattern with pricing.
- **`sessions.py`** — SQLite session store. `Session` has 4 fields: `session_id`, `messages`, `clone_path`, `last_profile`. DB at `aibuilder/data/sessions.db` (override with `AIBUILDER_DB` env var). No schema migrations in v1.
- **`static/index.html`** — Single-file chat UI adapted from deploy-agent: single-column layout, no dropzone, no file upload. Reuses the markdown renderer, Bajan typing messages, and GovTech palette.

### Things that will bite you

- **Port 8001** — aibuilder runs on `:8001`; deploy-agent on `:8000`. Both can run in parallel.
- **Repo clones grow unbounded** — `clone_repo` shells out to `git clone --depth=1`. The dir at `aibuilder/tmp/repos/` is gitignored but never auto-pruned in v1.
- **Repo guards** — `AIBUILDER_MAX_FILES` (default 5000), `AIBUILDER_MAX_SIZE_MB` (default 500), `AIBUILDER_TMP_DIR` (default `aibuilder/tmp/repos`). Read at call-time so tests can monkeypatch.
- **Path-traversal hardening** — The URL regex's `[\w.-]+` allows `.` and `..` as owner/repo segments. `clone_repo` explicitly rejects those and verifies the resolved target sits inside the session dir. If you swap the regex, keep both checks.
- **The pattern catalog is the brain, not the LLM** — `_CATALOG` in `patterns.py` is the source of truth. `SYSTEM_PROMPT` says "NEVER invent AWS services that `recommend_architecture` did not return. NEVER invent dollar amounts that `estimate_cost` did not return." Don't loosen those rules without a plan.
- **`is_fallback: true` always (v1)** — Cost estimates come from `_FALLBACK_PRICES`. Phase 1.5 will add live AWS Pricing API lookups; the `CostEstimate.is_fallback` field is reserved so that change is purely additive.
- **No file uploads** — Unlike deploy-agent, aibuilder has no `/api/upload/` endpoint and no dropzone in the UI. Only chat input.
- **Test fixtures live at `aibuilder/tests/fixtures/`** — Sample repos (`static_site/`, `node_api/`, `python_api/`, `dockerized_web/`, `spa_with_api/`, `fullstack_with_db/`, `worker/`) the analyzer parses during tests. Ruff is configured to exclude this directory; don't auto-format the fixtures.

## Design / plan archive

All design docs and implementation plans live under `docs/superpowers/`:

- **Specs:** `docs/superpowers/specs/`
- **Plans:** `docs/superpowers/plans/`

Plans shipped for **deploy-agent** (in order): the original hardening pass (Plan A), upload-preflight + destroy workflow (Plan B), update flow + duplicate-download preflight + file re-injection (Plan C), empty-bucket-before-destroy (Plan D), GovTech UI restyle (Plan E), markdown renderer + capability greeting (Plan F), Bajan loading messages (Plan G).

Plans shipped for **aibuilder**: Phase 1 — repo analysis + AWS cost estimate (`docs/superpowers/plans/2026-06-02-aibuilder-phase1.md`, spec at `docs/superpowers/specs/2026-06-02-aibuilder-design.md`).

**Open / pending:**

- **Plan H — rate limits.** Spec described in conversation, not yet written to `docs/`. Per-session daily cap on `deploy_infrastructure` (default 20) + global daily cap (default 100). Returns the standard `{summary, details}` shape on hit.
- **Sub-project B — host the agent in AWS.** Decomposed into B1 (MVP self-host on App Runner/ECS, S3 backend for tofu state, ECR, GitHub Actions deploy) → B2 (cross-account deploy via STS) → B3 (real auth) → B4 (audit + cost guardrails) → B5 (custom domain). LLM backend will be **Bedrock** (`anthropic.AnthropicBedrock`) not Anthropic direct, for GovTech compliance / single-bill / IAM-only auth. **Currently paused** until a concrete trigger (teammate access, GovTech requirement). Design notes are in this CLAUDE.md and the conversation history; will get a proper spec when picked up.

## Test count baseline

- `cd deploy-agent && make check` → **67 tests passing**.
- `cd aibuilder && make check` → **45 tests passing**.

New work should grow these counts, not shrink them.
