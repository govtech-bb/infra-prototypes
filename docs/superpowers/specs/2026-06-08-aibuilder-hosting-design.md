# aibuilder hosting on GovTech AWS — MVP

**Status:** Draft
**Date:** 2026-06-08
**Owner:** ChristopherN.Corbin@govtech.bb

## Problem

`aibuilder/` (the chat bot at port 8001) runs only on Chris's laptop today. That's fine for development but:

1. Teammates can't try it without running it themselves.
2. The Anthropic API key is on a personal account — wrong billing path for a GovTech tool.
3. Every test session against a real repo eats local clone-dir disk and is ephemeral — no record of what was asked or recommended.
4. The strategic goal (per project memory) was always to host this in GovTech AWS once a concrete trigger arrived. Internal usage is that trigger.

We want a single shared URL teammates can hit, billed against a GovTech account, with the LLM running via Bedrock (the compliance-friendly path AWS sells to government workloads), and the local dev workflow identical to the hosted workflow so there's no "works on my laptop, breaks in prod" surprise.

## Goals (MVP)

1. **Single hosted URL** accessible from anywhere with the right token.
2. **Docker-first**: the same image runs locally (`docker compose up`) and in the cloud (pulled from ECR by ECS Fargate). No code paths that only fire in one environment.
3. **Bedrock everywhere** as the LLM backend. Local dev uses the developer's SSO credentials to call Bedrock; cloud uses the ECS task role. No Anthropic-direct fallback — strict parity, even if local dev is now metered per token.
4. **Bearer-token auth** on the chat API surface so the URL isn't publicly readable. Token rotates via SSM Parameter Store.
5. **Push-to-deploy** via GitHub Actions: merge to main → image built → ECR pushed → ECS task updated → live in under five minutes.
6. **State persistence** for sessions across container restarts (EFS-mounted SQLite — no schema migration, same `SqliteSessionStore`).
7. **Honest dogfooding**: the architecture chosen is one aibuilder itself would recommend for a Python+container+state workload — ECS Fargate + ALB + RDS-or-EFS (here EFS). When the bot says "this is the pattern for your repo," that pattern actually runs the bot.

## Non-goals (MVP — explicit follow-up specs later)

- **Custom domain.** Use the ALB DNS name (`aibuilder-<rand>.us-east-1.elb.amazonaws.com`) for MVP. Route53 + ACM + a `*.govtech.bb` subdomain is a separate spec.
- **Multi-environment** (staging vs prod). Single environment for MVP — what we ship IS prod, gated only by the bearer token.
- **Cognito / SSO auth.** Bearer token is the entire auth story. Replacing with Cognito + ALB authentication action is a separate spec when we want per-user identity.
- **Cross-account isolation.** Everything lives in one GovTech AWS account. No STS assume-role into other accounts (aibuilder doesn't create resources, only reads pricing, so cross-account is unnecessary at MVP).
- **DynamoDB session store.** SQLite-on-EFS is fine for single-task service. Migration to DynamoDB is a separate spec if we scale beyond one task.
- **Billing alerts / cost guardrails / CloudWatch dashboards.** Manual monitoring for MVP. Build alarms later when we have a real usage baseline.
- **Live-pricing extension to more services.** That's the unrelated tactical track (`b033ea9` and follow-ups). MVP hosting ships with whatever live-pricing coverage exists at deploy time.

## High-level architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  GitHub: github.com/christophercorbin/infra-prototypes           │
│    ├─► push to main: build container, push to ECR, update ECS    │
│    └─► PR: build container, run `make check` inside it           │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  GovTech AWS account (sandbox-class — TBD pick at deploy)        │
│                                                                  │
│  Amazon ECR ─────────► aibuilder:latest, aibuilder:sha-<7chr>    │
│       │                                                          │
│       ▼ pulled at task start                                     │
│                                                                  │
│  ECS Fargate Service (1 task, public subnets across 2 AZs)       │
│    └─► container: uvicorn + aibuilder FastAPI app                │
│         │                                                        │
│         ├─► Amazon Bedrock (Anthropic Claude via IAM)            │
│         ├─► AWS Pricing API (pricing:GetProducts read-only)      │
│         ├─► EFS mount at /aibuilder/data/sessions.db             │
│         └─► CloudWatch Logs (7-day retention)                    │
│                                                                  │
│  Application Load Balancer (public-facing)                       │
│    ├─► HTTPS:443 via ACM cert                                    │
│    ├─► Health check on GET /api/health                           │
│    └─► Forwards :443 → ECS task :8001                            │
│                                                                  │
│  SSM Parameter Store: /aibuilder/auth-token (SecureString)       │
│       └─► injected as AIBUILDER_TOKEN env var via ECS secrets    │
└──────────────────────────────────────────────────────────────────┘
```

## Components

### 1. Container image (`aibuilder/Dockerfile`)

```dockerfile
FROM python:3.13-slim AS base
WORKDIR /aibuilder

RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
```

Key choices:
- `git` is installed because `tools.clone_repo` shells out to it.
- Single-stage build (no compile step) — keeps image under 200 MB.
- App runs as the default `root` user for MVP simplicity. Future hardening: dedicated non-root user with `chown` on `/aibuilder/data/`.
- No `--reload` in CMD — that's a dev-only flag in `run.sh`. Hot reload happens via `docker compose` volume mounts locally; cloud runs the immutable image.

### 2. Local dev: `aibuilder/docker-compose.yml`

```yaml
services:
  aibuilder:
    build: .
    ports:
      - "8001:8001"
    volumes:
      - .:/aibuilder                       # code reload
      - aibuilder_data:/aibuilder/data     # SQLite persists across `down`
      - ~/.aws:/root/.aws:ro               # SSO cache for boto3 (read-only)
    environment:
      - AWS_PROFILE=${AWS_PROFILE:-personal-default}
      - AWS_REGION=us-east-1
      - AIBUILDER_TOKEN=${AIBUILDER_TOKEN:-local-dev-token}
      - AIBUILDER_BEDROCK_MODEL=${AIBUILDER_BEDROCK_MODEL:-anthropic.claude-opus-4-6-v1:0}

volumes:
  aibuilder_data:
```

Local dev requires `aws sso login --sso-session personal` (or `govtech`) active in the shell — boto3 inside the container reads creds from the mounted `~/.aws/`. **Same code path as cloud**; only the credential source differs (SSO cache file vs IAM task role).

### 3. ECR repository

- Repo name: `aibuilder`
- Lifecycle policy: keep last 10 tagged images + all `latest` + delete untagged after 7 days. Stops the registry from growing unbounded.
- Image scanning enabled (basic — AWS-managed scan on push).

### 4. ECS cluster + service + task definition

- Cluster: `aibuilder-cluster` (Fargate-only, no EC2 capacity providers).
- Task definition: 0.25 vCPU / 0.5 GB. Single container, exposes port 8001.
- Service: 1 desired task, no autoscaling. Deployment circuit breaker enabled so a bad deploy auto-rolls-back. Minimum healthy 0%, maximum 200% — replace-then-kill on deploys.
- Task execution role: `AmazonECSTaskExecutionRolePolicy` (ECR pull, CloudWatch Logs write).
- Task role (the app's runtime identity):
  - `bedrock:InvokeModel` on the specific Claude model ARN
  - `pricing:GetProducts`, `pricing:GetAttributeValues`, `pricing:DescribeServices` on `*`
  - `ssm:GetParameter` on `/aibuilder/auth-token` (for the secret injection — though ECS handles this via the task definition's `secrets:` field, not at runtime by the app)
  - `elasticfilesystem:ClientMount`, `elasticfilesystem:ClientWrite` on the EFS file system ARN

### 5. Application Load Balancer + listener + target group

- ALB scheme: `internet-facing`, 2 public subnets across 2 AZs (same VPC as Fargate).
- Listener: 443 HTTPS using an ACM cert. Default action: forward to target group.
- Listener: 80 HTTP → permanent redirect to 443.
- Target group: type `ip`, port 8001, health check `GET /api/health` expecting 200.
- ACM cert: provisioned in the same region (us-east-1), validated via DNS. The cert is for the ALB's auto-generated DNS name for MVP — when we add a custom domain later, a new cert covers `*.govtech.bb` or similar.

### 6. EFS file system (session persistence)

- Single EFS file system, one mount target per AZ.
- Access point: `/aibuilder/data/` with POSIX UID/GID 0 (root, matching the container's default user).
- Performance mode: General Purpose (default — fine for SQLite at this scale).
- Throughput mode: Bursting (default — free for the storage we'll use).
- ECS task definition mounts the access point at `/aibuilder/data` inside the container — same path the local `SqliteSessionStore` already uses (`Path(__file__).parent / "data" / "sessions.db"`).
- Backup: AWS Backup default plan covers EFS automatically (1 daily backup, 35-day retention). Sessions aren't critical data but the backup is free in this volume range.

### 7. Bedrock LLM call (code change in `aibuilder/agent.py`)

Today:
```python
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
```

New:
```python
import anthropic
client = anthropic.AnthropicBedrock(
    aws_region=os.environ.get("AWS_REGION", "us-east-1"),
)
```

Model ID changes:
- Current: `claude-opus-4-6`
- Bedrock: `anthropic.claude-opus-4-6-v1:0` (Bedrock prefixes with `anthropic.` and suffixes with version)
- Made configurable via `AIBUILDER_BEDROCK_MODEL` env var so model upgrades don't require a code change

`anthropic.AnthropicBedrock` uses boto3 under the hood — picks up credentials from the standard chain (env vars, ~/.aws/credentials, SSO cache, IAM task role). No code changes for credentials.

### 8. Pricing API access

No code change. Existing `pricing._live_price_lambda()` and `_live_price_s3()` already use `boto3.client("pricing", region_name="us-east-1")`. The IAM task role grants the needed permissions. Same code, same boto3 SDK, different credential source.

### 9. Bearer-token auth middleware (new in `aibuilder/app.py`)

```python
from fastapi import FastAPI, Request, HTTPException

_TOKEN = os.environ.get("AIBUILDER_TOKEN")

@app.middleware("http")
async def require_bearer_token(request: Request, call_next):
    # Static assets + health endpoint stay public so the ALB health check
    # and the chat UI HTML can load without auth.
    if request.url.path.startswith(("/api/health", "/static/")) or request.url.path == "/":
        return await call_next(request)
    if not _TOKEN:
        return await call_next(request)  # local dev with no token set — open
    header = request.headers.get("authorization", "")
    if header != f"Bearer {_TOKEN}":
        raise HTTPException(401, "Missing or invalid Authorization header")
    return await call_next(request)
```

UI change in `static/index.html`: read the token from a `<meta name="aibuilder-token" content="...">` injected by `app.py` at template-render time, OR (simpler for MVP) prompt the user to paste the token into localStorage on first load. The latter avoids any server-side templating.

### 10. SSM Parameter Store

- Parameter: `/aibuilder/auth-token`, type `SecureString`, KMS-encrypted with the AWS-managed `aws/ssm` key.
- ECS task definition's `secrets:` block maps the parameter to the `AIBUILDER_TOKEN` environment variable. ECS resolves the secret at task launch — the value never appears in the task definition JSON.
- Rotation: manual — `aws ssm put-parameter --overwrite ...` then redeploy the service. Future: Secrets Manager with automatic rotation if we care.

### 11. CloudWatch Logs

- Log group: `/ecs/aibuilder`, retention 7 days (set explicitly — never-expire is the default cost trap aibuilder itself warns about).
- Task definition's `logConfiguration` ships container stdout/stderr to this group automatically.

## Data flow (request lifecycle)

```
Browser
   │ HTTPS GET / + paste token into localStorage on first load
   ▼
ALB (443) ── TLS terminates here, forwards plain HTTP :8001
   │
   ▼
Fargate task (uvicorn :8001)
   │ Middleware checks Authorization: Bearer header
   ▼
FastAPI route handler
   │
   ├─► sessions.py: read/write /aibuilder/data/sessions.db on EFS
   │
   ├─► tools.clone_repo: git clone into /aibuilder/tmp/repos/<session>/
   │       (writes to task-local ephemeral storage — 20 GB free tier)
   │
   ├─► agent.run_agent_loop:
   │       anthropic.AnthropicBedrock.messages.create(...)
   │       boto3 → STS → IAM task role → Bedrock → Claude response
   │
   └─► pricing._live_price_*: boto3.client("pricing").get_products(...)
           Same credential chain → Pricing API
   │
   ▼
Response → ALB → Browser
```

## Local vs cloud parity

| Concern | Local (`docker compose up`) | Cloud (ECS Fargate) |
|---|---|---|
| Container image | Built from same Dockerfile | Same Dockerfile, built by CI, pulled from ECR |
| Code path | Identical (one branch in `agent.py`) | Identical |
| LLM backend | Bedrock (via mounted SSO creds) | Bedrock (via IAM task role) |
| Pricing API | Same boto3 call | Same boto3 call |
| Sessions | SQLite at `/aibuilder/data/sessions.db` (Docker volume) | SQLite at `/aibuilder/data/sessions.db` (EFS mount) |
| Auth | `AIBUILDER_TOKEN` from `.env` (or unset = open for dev) | `AIBUILDER_TOKEN` injected by ECS from SSM |
| Port | 8001 (docker-compose host:container) | 8001 (Fargate port, ALB routes to it) |

The single "what's different" is credential source — and that's resolved at the boto3 layer, transparent to application code. No `if os.environ.get("CLOUD"):` branches anywhere.

## Code changes in `aibuilder/`

Minimal, deliberately:

1. `agent.py`: swap `anthropic.Anthropic()` → `anthropic.AnthropicBedrock(...)` and model ID. ~3 lines.
2. `app.py`: add bearer-token middleware. ~10 lines.
3. `static/index.html`: add localStorage token prompt on first chat send. ~15 lines.
4. New `Dockerfile`. ~15 lines.
5. New `docker-compose.yml`. ~20 lines.
6. New `.dockerignore`: exclude `.venv/`, `data/`, `tmp/`, `__pycache__/`. ~10 lines.

Tests:
- Existing tests run inside the container (`docker compose run aibuilder make check`) — that's the CI command.
- New test for the bearer middleware: missing token → 401; valid token → 200; static assets → no auth required.
- New test mocking `anthropic.AnthropicBedrock` so `run_agent_loop` tests don't change shape.

## New repo additions outside `aibuilder/`

```
aibuilder/
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
└── infra/                          ← NEW
    ├── Makefile                    ← mirrors infra/Makefile shape
    ├── stacks/
    │   └── aibuilder-hosting/
    │       ├── main.tf
    │       ├── variables.tf
    │       ├── outputs.tf
    │       ├── ecr.tf
    │       ├── ecs.tf
    │       ├── alb.tf
    │       ├── efs.tf
    │       ├── iam.tf
    │       ├── ssm.tf
    │       └── networking.tf
    └── README.md

.github/workflows/
└── aibuilder-deploy.yml            ← NEW: build, push, deploy
```

(Existing `infra/` for deploy-agent stays untouched. `aibuilder/infra/` is a sibling, not a child of `infra/`.)

## GitHub Actions workflow shape

`.github/workflows/aibuilder-deploy.yml`:

- Trigger: `push: branches: [main]` (path filter to `aibuilder/**`); `pull_request:` (same path filter, build+test only)
- Jobs:
  1. **test** (always): build image, `docker run aibuilder make check`. Fail-closed.
  2. **deploy** (main only, depends on test): configure AWS creds via OIDC role (no long-lived access keys); `docker build`, tag with `latest` + `sha-${GITHUB_SHA::7}`; `aws ecr get-login-password | docker login`; `docker push` both tags; `aws ecs update-service --force-new-deployment` to roll the service.

Auth from GitHub to AWS: configure a GitHub OIDC IdP in the AWS account once, then a dedicated `aibuilder-deploy` role that trusts the GitHub repo's main branch. No access keys checked in.

## Cost estimate (MVP, monthly)

| Service | Approx. monthly | Notes |
|---|---|---|
| ECS Fargate | $9.00 | 0.25 vCPU / 0.5 GB, 24/7 |
| Application Load Balancer | $16.00 | Fixed + negligible LCU |
| ECR | $0.10 | ~1 GB stored; first 500 MB free |
| EFS Standard | $0.30 | <1 GB sessions data, Bursting |
| CloudWatch Logs | <$1.00 | 7-day retention, prototype traffic |
| ACM cert | $0.00 | Free for AWS-issued certs on ALB |
| SSM Parameter Store | $0.00 | Standard tier free |
| Data transfer out | $1.00 | A few GB/mo egress for chat responses |
| **Subtotal: AWS infra** | **~$27/mo** | |
| Bedrock (Claude Opus 4.6) | metered | ~$15/1M input tokens, ~$75/1M output (per current Bedrock pricing). At low team usage probably $5–30/mo. |
| **Total estimate** | **~$30–60/mo** | infra + Bedrock at light-to-moderate usage |

aibuilder's own catalog would price this as `fullstack_with_db` minus RDS plus EFS ≈ $25/mo. The estimate above adds Bedrock usage which isn't part of the catalog. Honest dogfooding.

## Networking + security

- **VPC:** use the default VPC. Two public subnets across 2 AZs. No new VPC creation — keeps the IaC simple. Hardening (private subnets + NAT) is a follow-up if security review requires it.
- **Security groups:**
  - `aibuilder-alb-sg`: ingress `0.0.0.0/0:443`; egress to `aibuilder-task-sg:8001` only.
  - `aibuilder-task-sg`: ingress from `aibuilder-alb-sg:8001` only; egress to `0.0.0.0/0:443` (for Bedrock / Pricing / GitHub clone via HTTPS) and to `aibuilder-efs-sg:2049`.
  - `aibuilder-efs-sg`: ingress from `aibuilder-task-sg:2049` only.
- **IAM principle:** least-privilege. Task role gets exactly the actions listed in §4; nothing on `*` except where unavoidable (Pricing API doesn't support resource-level ARNs).
- **No private subnets, no NAT Gateway** — saves ~$32/mo, fine for MVP since the task only needs outbound HTTPS to AWS APIs + GitHub, all of which are reachable via IGW.
- **TLS everywhere:** ACM cert on ALB; HTTP→HTTPS redirect at the ALB; container itself runs plain HTTP on 8001 inside the VPC (ALB→task hop is intra-VPC).
- **No WAF for MVP.** Add when we have real users + a security review. Token-based auth is the only protection against random scanners.

## Testing strategy

- **Unit tests** (`make check` inside the container) cover existing aibuilder code paths plus the new bearer-token middleware. Same 109+ tests baseline.
- **Local docker-compose smoke test**: `docker compose up`, `curl -H "Authorization: Bearer local-dev-token" http://localhost:8001/api/session` should return a session ID. Add this to `aibuilder/scripts/smoke-test.sh` (already exists for the local-uvicorn case).
- **Bedrock integration test**: deliberately deferred. The `anthropic.AnthropicBedrock` client is the SDK — we trust Anthropic's tests. Our integration coverage is via the smoke test that does one full chat turn.
- **Deploy smoke test**: after a successful deploy, the GitHub Actions workflow hits the ALB's `/api/health` endpoint until it returns 200, otherwise fails the deploy (and the ECS circuit breaker auto-rolls back).
- **Pricing API in tests**: existing `conftest.py` fixture already mocks `pricing.boto3.client` to raise — tests don't depend on AWS connectivity. No change needed.

## Open items deferred to later phases (B2-B5 style)

These are explicit follow-up specs, not "TODO" rot:

- **B2** — **Custom domain** + Route53 + multi-region cert. ~1 spec, small.
- **B3** — **Cognito + SSO auth** to replace the bearer token; per-user audit trail. Medium spec.
- **B4** — **Cross-account architecture** if aibuilder ever recommends-and-then-deploys into other GovTech accounts (would need STS assume-role machinery).
- **B5** — **Cost guardrails**: Bedrock spend budget alerts, ECS overprovisioning alerts, CloudWatch dashboards.
- **B6** (new) — **DynamoDB session store** to allow multi-task service (horizontal scaling beyond 1 task; current SQLite+EFS is single-writer).
- **B7** (new) — **Live-pricing service expansion** is unrelated to hosting but worth tracking — independent commit cadence on `pricing.py`.

## Open questions for the user

1. **Which GovTech account?** Memory lists `govtech-alpha-prod` (production), `govtech-mgmt` / `govtech-log-archive` / `govtech-network-edge` (read-only). For a sandbox-class deployment, none of these fit. **Likely action: create or designate a new `govtech-sandbox` or `govtech-dev` account before deploy**, or override and use `govtech-alpha-prod` if no sandbox exists.
2. **Bedrock model availability:** Claude Opus 4.6 must be enabled in the target account's Bedrock console before the task can call it. This is a one-time toggle (and may require AWS support ticket in some regions). Surface at deploy time.
3. **GitHub Actions OIDC role:** the deploy role's trust policy needs the GitHub repo's identifier baked in. We're using `christophercorbin/infra-prototypes` — confirm at deploy time. (Trivial — just mentioning so it's not a surprise.)
