# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

Two-part prototype for "deploy a static website to AWS via natural-language chat":

- `infra/` — OpenTofu (Terraform-compatible) stack that provisions an S3 + CloudFront static-site setup. Usable standalone via `make`.
- `deploy-agent/` — FastAPI app that exposes a chat UI; Claude (`claude-opus-4-6`) is given two tools (`deploy_infrastructure`, `upload_files`) that shell out to `tofu` and `boto3` to drive the `infra/` stack.

The two pieces are siblings, not nested — `deploy-agent/tools.py` resolves `INFRA_DIR` as `../infra` relative to itself (`tools.py:14-17`).

## Commands

### Deploy agent (chat UI)
```bash
cd deploy-agent
export ANTHROPIC_API_KEY=sk-ant-...
export AWS_PROFILE=...        # or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
./run.sh                      # installs deps if missing, runs uvicorn on :8000
```
The `run.sh` script gates on `ANTHROPIC_API_KEY` and warns (does not fail) on missing AWS creds. Open http://localhost:8000.

### Infra stack (manual / standalone)
From `infra/` — all targets accept `PROJECT=` and `ENV=` overrides; `STACK` defaults to `static-website`:
```bash
make init                                       # tofu init in stacks/$(STACK)
make plan    PROJECT=myapp ENV=chris-test
make deploy  PROJECT=myapp ENV=chris-test       # tofu apply -auto-approve, prints site_url
make upload  PROJECT=myapp ENV=chris-test       # syncs ./dist to S3 + invalidates CF
make destroy PROJECT=myapp ENV=chris-test
```
The `upload` target reads `bucket_name` and `cloudfront_distribution_id` from `tofu output -raw` of the currently selected workspace, so run it from the same shell context as the deploy.

## Architecture

### Agent loop (`deploy-agent/agent.py`)
`run_agent_loop` is a hand-rolled tool-use loop with `MAX_AGENT_ITERATIONS = 15` (overridable in tests). Each turn:
1. Calls `client.messages.create` with the cumulative `session["messages"]`.
2. Appends the assistant's `content` blocks to history.
3. On `stop_reason == "end_turn"`, returns the first text block.
4. On `stop_reason == "tool_use"`, executes every `tool_use` block via `tools.execute_tool`, packages results as a single `user` message of `tool_result` blocks, and loops.

Sessions are persisted in a SQLite file at `deploy-agent/data/sessions.db` via `SqliteSessionStore` in `sessions.py`. Schema is single-table; `project_name` and `env` are denormalized columns extracted from `deployment` so `scripts/destroy_all.py` can `SELECT` them without parsing JSON. Override the path with `DEPLOY_AGENT_DB` env var (used by tests).

When files are uploaded via `/api/upload/{session_id}`, the file list is **injected once** into the next user message as `[Uploaded files: …]` (`app.py:170-177`). The agent uses that as its cue to call `upload_files` after `deploy_infrastructure`.

### Tool implementations (`deploy-agent/tools.py`)
Two tools, both synchronous and shell-based:

- **`deploy_infrastructure`** runs `tofu init`, then `tofu workspace new/select <project>-<env>`, then `tofu apply` with `-var=` flags, then parses `tofu output -json` (`tools.py:85-147`). Workspaces isolate state per deployment — that's the only multi-tenant boundary, since `backend.tf` keeps state local.
- **`upload_files`** uses `boto3` to `upload_file` everything under the session's `upload_dir`, guesses MIME types via `mimetypes`, then calls `cloudfront.create_invalidation` with `Paths: ["/*"]` (`tools.py:150-198`).

Errors are returned as `{"error": "..."}` dicts (never raised) so the agent can surface them back to the user. Stderr is truncated to the last 2-3 KB.

### Terraform stack (`infra/stacks/static-website/`)
Composes two modules: `modules/s3-static-site` (private bucket + public-access-block, optional versioning) and `modules/cloudfront` (distribution + OAC, SPA mode toggles `custom_error_response` for 403/404 → `index.html`).

**Watch this:** the S3 bucket policy granting CloudFront read access lives in the **stack**, not the s3 module (`stacks/static-website/main.tf:59-82`). This is deliberate — putting it in the s3 module would create a circular dependency (S3 needs CF's ARN, CF needs S3's regional domain). When adding new resources, keep this split in mind.

Tags flow from `locals.common_tags` in the stack and are applied via the AWS provider's `default_tags`, so module resources inherit them automatically.

### State backend
`backend.tf` is intentionally commented out — prototypes use local state. The file contains a ready-to-uncomment S3 backend block for graduating to shared state. If you migrate, run `tofu init` to migrate state.

## Conventions

- **Default env** for agent-driven deploys is `proto` (per the system prompt in `app.py:48`); the `Makefile` defaults to `dev`. Don't mix them — they create different workspaces.
- **Bucket naming** is `${project_name}-${env}-static` (`modules/s3-static-site/main.tf:2`), so `project_name` collisions across envs are fine but cross-env collisions are not.
- **`is_spa=true`** turns CloudFront 403/404s into 200s serving `index.html` — only enable for SPAs, otherwise it masks real 404s.
- `price_class` defaults to `PriceClass_100` (US/EU edges only) for prototype cost.
- The agent model is pinned to `claude-opus-4-6` in `app.py:81`. To change, edit there — no env override.

## Things that will bite you

- The agent calls `tofu`, not `terraform` — make sure OpenTofu is installed and on PATH.
- `tofu workspace new` is run unconditionally and ignores its non-zero exit code (workspace already exists is expected). If you see weird state, check `tofu workspace list` in `infra/stacks/static-website/`.
- ACM certs for CloudFront **must** be in `us-east-1` regardless of `aws_region` — see `acm_certificate_arn` in `variables.tf:65`.
- File uploads in `/api/upload/{session_id}` preserve relative paths and reject `..` traversal / absolute paths with HTTP 400. `examples/sample-site/assets/logo.svg` is the regression fixture — if you break path preservation, the smoke test will fail.
