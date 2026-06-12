# aibuilder deploy + modify — design

**Date:** 2026-06-11
**Status:** Approved (brainstormed in session; approach + waves locked with Chris)
**Scope:** aibuilder gains the ability to deploy analyzed repos into AWS and support post-deploy modification. Target is *every* catalog pattern, delivered in waves. This spec covers the whole architecture; Waves 0–1 are the first implementation plan.

## Problem

aibuilder analyzes a GitHub repo, recommends an AWS architecture, and estimates cost — then stops. Teammates prototyping with Claude Code have no path from "estimate looks good" to "it's live", except asking Chris to deploy manually. The team wants:

1. **Deploy:** "yes, deploy it" in chat → the recommended architecture goes live in `govtech-sandbox` with a URL.
2. **Modify:** after deploy, two loops:
   - *Code loop:* teammate keeps iterating in their own Claude Code session → commit → push → trigger redeploy (without opening the aibuilder UI).
   - *Infra loop:* chat-driven config changes (SPA mode, sizing, price class) on the live deployment.

## Decisions (made during brainstorming)

| Decision | Choice | Why |
|---|---|---|
| Deploy scope target | All catalog patterns | Team ambition; forces pattern-pluggable design from day one |
| Delivery | Waves (static → containers → DB/workers) | Each wave is shippable; risk stays bounded |
| Engine | Hosted aibuilder runs tofu in its own Fargate task | Zero AWS setup for teammates; matches deploy-agent's proven model |
| Target account | `govtech-sandbox` (672203047922) | Where aibuilder itself lives; sandbox blast radius |
| Modify scope | Redeploy-latest-code + infra knobs | "AI edits the app code" explicitly out of scope (separate product) |
| Teammate loop | Bearer-authed redeploy endpoint their Claude can curl | Teammates stay in their own Claude Code sessions |
| Private repos | Yes — org-scoped fine-grained PAT in SSM | Most real prototypes are private in govtech-bb |
| Guardrails | 14-day TTL auto-expiry + deploy caps | Kills the forgotten-prototype cost leak; Plan H design finally ships (in aibuilder) |

## Hard constraints discovered

1. **No synchronous deploys.** The hosted request path (CloudFront → ALB → uvicorn) times out long before a multi-minute `tofu apply` finishes. All deploy/destroy/redeploy operations are **async jobs**; chat returns immediately and status is polled.
2. **No Docker builds in Fargate.** The task has no Docker daemon. Container image builds (and `npm run build` for built static sites) belong in **AWS CodeBuild** — Wave 2. Wave 1 deploys plain static repos only.
3. **State must outlive the task.** Local tofu state dies with the container. Deployed-prototype state lives in S3 with DynamoDB locking, one state key per deployment.

## Architecture

### Components (all inside `aibuilder/`)

- **`deployments.py`** — SQLite-backed deployment records: `deployment_id`, `session_id`, `repo_url`, `pattern`, `project_name`, `env`, `status`, `outputs` (JSON: site_url, bucket, distribution…), `expires_at`, `last_error`, timestamps. Status machine: `queued → cloning → applying → live` | `failed`; plus `destroying → destroyed`, `expired`. Idempotent ALTER-based migrations (deploy-agent pattern).
- **`jobs.py`** — in-process async job runner: asyncio queue + single worker started in FastAPI lifespan. Deploys serialize (fine for a small team). Startup recovery: in-flight statuses → `failed("interrupted by restart")`; tofu state in S3 remains the source of truth so retry is always safe.
- **`deploy_stacks/`** — the pluggable seam. `StackSpec(stack_dir, build_vars, allowed_knobs, preflight)` registered in `STACK_REGISTRY: dict[pattern, StackSpec]`. Unknown pattern → "not deployable yet" listing supported patterns, *generated from the registry* (never hand-maintained — catalog-iteration lesson). Stack tofu sources ship inside the Docker image.
- **`limits.py`** — deploy caps counted from the deployments table: `AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY` (default 10), `AIBUILDER_MAX_DEPLOYS_GLOBAL_DAY` (default 50). Failures use the standard `{summary, details}` shape.
- **New agent tools** — `deploy_repo`, `get_deployment_status`, `redeploy`, `modify_deployment`, `extend_deployment`, `destroy_deployment` (two-phase confirm, ported from deploy-agent), `list_deployments` (with TTL remaining). `_classify_error` ported for friendly tofu/AWS error summaries.
- **HTTP endpoints** — `POST /api/deployments/{id}/redeploy` (202 + status URL) and `GET /api/deployments/{id}`, behind the existing bearer middleware. A copy-paste CLAUDE.md snippet documents the teammate flow (commit → push → curl redeploy).
- **TTL reaper** — hourly asyncio task; past-`expires_at` deployments get a destroy job and `expired` status. `extend_deployment` resets the clock (+14 days). `list_deployments` warns under 48h.

### Execution model (deploy job)

1. Caps check → create deployment row (`queued`) → enqueue → chat replies "started, ask me for status".
2. Job: clone (re-using session clone when fresh) → pattern preflight → per-deployment workdir `/aibuilder/data/deploys/<id>` with `TF_DATA_DIR` → `tofu init -backend-config key=deployments/<project>-<env>.tfstate` → `apply` with registry-built vars → parse outputs → post-apply content sync (W1: boto3 S3 sync + CloudFront invalidation) → `live`.
3. Redeploy = re-clone + re-sync + invalidate (no tofu). Modify = re-apply with changed knob vars (per-pattern allowlist). Destroy = empty bucket → `tofu destroy` → `destroyed`.

### Hosting-stack additions (`aibuilder/infra/stacks/aibuilder-hosting/`)

- S3 state bucket `aibuilder-deploy-state-672203047922` (versioned, encrypted, BPA) + DynamoDB lock table `aibuilder-deploy-lock`.
- SSM `/aibuilder/github-token` (SecureString, manual put, `ignore_changes`) → task secret `AIBUILDER_GITHUB_TOKEN`. Clone retries with `x-access-token:` URL on auth failure; token scrubbed from all logs/errors.
- Task-role IAM grows per wave behind resource-name scoping: deployed resources use an `aibd-` name prefix, and provisioning policies are scoped to that prefix so the deploy engine cannot touch the hosting stack or anything else in the sandbox.
- `tofu` binary baked into the image (pinned 1.8.x, matching CI).

## Waves

- **W0 — foundation:** state backend, deployments store, job runner, registry, caps, private-repo clone, tofu-in-image. No user-visible deploys.
- **W1 — static sites:** `infra/stacks/static-website` copied to `deploy_stacks/static-website/` (S3 backend block added, `aibd-` naming). Full loop: deploy → live URL → modify (`is_spa`, `price_class`) → redeploy → destroy → TTL expiry. Plain static only; build-required repos get a friendly "Wave 2" rejection.
- **W2 — containers + builds:** CodeBuild project for user-repo builds (docker images *and* npm-built static sites) → ECR. New `container-app` stack: Fargate task + **shared ALB with host-header routing** (one ALB for all prototypes — not $16/mo each). Patterns: `dockerized_web`, `node_api`, `python_api`, `tiny_container`, `internal_tool`, built `static_site`/`spa_with_api`.
- **W3 — data + workers:** `fullstack_with_db` (DynamoDB first; RDS later with cost-ceiling conversation), `worker`/`queue_worker`/`workflow_worker` via ECS scheduled/queue-driven tasks.
- **W+ — deeper teammate loop:** GitHub webhook auto-redeploy (HMAC-verified), per-deployment tokens, architecture-migration ("it needs a database now") flows.

## Error handling

- Tool failures never raise; `{summary, details}` everywhere; agent reports summary, offers details (deploy-agent convention).
- `_classify_error` maps tofu/AWS stderr to friendly messages; pattern order matters (port as-is, extend for new stack errors).
- Job failures: status `failed` + `last_error`; chat surfaces it on next status ask.
- GitHub token scrubbed from any stderr before storage/logging.

## Testing

- Units: registry lookups + generated supported-list, caps math, TTL/reaper selection, deployments store CRUD + migration, job runner with a fake executor.
- Tool tests with mocked subprocess + moto where useful (crib deploy-agent's test layout).
- Live smoke per wave: deploy a fixture-grade public repo end-to-end in sandbox → assert URL serves → destroy → assert nothing left.
- `make check` stays green; **test count grows from 115, never shrinks.**

## Out of scope

AI-modifying user application code; deploys outside govtech-sandbox; per-user identity (bearer token stays team-shared); custom domains; production-grade SLAs. Live Pricing API (Phase 1.5) remains a separate, compatible effort.
