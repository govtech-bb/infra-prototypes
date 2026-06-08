# aibuilder hosting on GovTech AWS — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Host the existing `aibuilder/` chat bot in the `govtech-sandbox` AWS account so teammates can use it via a shared HTTPS URL, with the LLM running on Bedrock, bearer-token auth, push-to-deploy via GitHub Actions, and local-vs-cloud parity via Docker.

**Architecture:** Docker image built by GitHub Actions, pushed to ECR, run by an ECS Fargate task in public subnets behind an internal ALB, fronted by a CloudFront distribution (HTTPS via the free `*.cloudfront.net` cert). SQLite session DB persists on EFS. Bedrock and AWS Pricing API are reached via the task's IAM role. Bearer token comes from SSM Parameter Store and is enforced by a FastAPI middleware.

**Tech Stack:** Python 3.13 / FastAPI / uvicorn / boto3 / OpenTofu (Terraform-compatible HCL) / AWS Fargate / ALB / CloudFront / EFS / ECR / SSM / Bedrock / GitHub Actions OIDC.

**Spec:** `docs/superpowers/specs/2026-06-08-aibuilder-hosting-design.md`

---

## File map

**New:**
```
aibuilder/Dockerfile
aibuilder/.dockerignore
aibuilder/docker-compose.yml
aibuilder/infra/Makefile
aibuilder/infra/README.md
aibuilder/infra/stacks/aibuilder-hosting/providers.tf
aibuilder/infra/stacks/aibuilder-hosting/variables.tf
aibuilder/infra/stacks/aibuilder-hosting/outputs.tf
aibuilder/infra/stacks/aibuilder-hosting/locals.tf
aibuilder/infra/stacks/aibuilder-hosting/networking.tf
aibuilder/infra/stacks/aibuilder-hosting/ecr.tf
aibuilder/infra/stacks/aibuilder-hosting/efs.tf
aibuilder/infra/stacks/aibuilder-hosting/ssm.tf
aibuilder/infra/stacks/aibuilder-hosting/cloudwatch.tf
aibuilder/infra/stacks/aibuilder-hosting/iam.tf
aibuilder/infra/stacks/aibuilder-hosting/alb.tf
aibuilder/infra/stacks/aibuilder-hosting/ecs.tf
aibuilder/infra/stacks/aibuilder-hosting/cloudfront.tf
aibuilder/infra/stacks/aibuilder-hosting/oidc.tf
.github/workflows/aibuilder-deploy.yml
```

**Modified:**
```
aibuilder/agent.py          — swap Anthropic → AnthropicBedrock
aibuilder/app.py            — add bearer-token middleware
aibuilder/static/index.html — localStorage token prompt + Authorization header
aibuilder/tests/test_agent.py — update mocks for AnthropicBedrock
aibuilder/tests/test_app.py — add middleware tests
aibuilder/requirements.txt  — anthropic[bedrock] extra (if needed)
CLAUDE.md                   — add aibuilder/infra/ to repo structure
```

---

## Phase A — App code changes

### Task 1: Bearer-token auth middleware in `app.py`

**Files:**
- Modify: `aibuilder/app.py`
- Modify: `aibuilder/tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `aibuilder/tests/test_app.py`:

```python
def test_chat_without_token_returns_401_when_token_required(monkeypatch, tmp_path):
    """When AIBUILDER_TOKEN is set in the env, /api/chat must reject
    requests missing or mismatching the Authorization: Bearer header."""
    monkeypatch.setenv("AIBUILDER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-tests")
    monkeypatch.setenv("AIBUILDER_TOKEN", "s3cret")

    import importlib
    import app as app_module
    importlib.reload(app_module)
    client = TestClient(app_module.app)

    sid = client.get(
        "/api/session", headers={"Authorization": "Bearer s3cret"}
    ).json()["session_id"]

    # No header at all → 401
    response = client.post("/api/chat", json={"session_id": sid, "message": "hi"})
    assert response.status_code == 401

    # Wrong token → 401
    response = client.post(
        "/api/chat",
        json={"session_id": sid, "message": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_chat_with_correct_token_passes_middleware(monkeypatch, tmp_path):
    monkeypatch.setenv("AIBUILDER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-tests")
    monkeypatch.setenv("AIBUILDER_TOKEN", "s3cret")

    import importlib
    import app as app_module
    importlib.reload(app_module)
    client = TestClient(app_module.app)

    sid = client.get(
        "/api/session", headers={"Authorization": "Bearer s3cret"}
    ).json()["session_id"]

    with patch("app.run_agent_loop", return_value="ok"):
        response = client.post(
            "/api/chat",
            json={"session_id": sid, "message": "hi"},
            headers={"Authorization": "Bearer s3cret"},
        )
    assert response.status_code == 200


def test_health_endpoint_skips_auth(monkeypatch, tmp_path):
    """The ALB / CloudFront health checks hit /api/health without a token —
    that endpoint MUST stay open regardless of AIBUILDER_TOKEN."""
    monkeypatch.setenv("AIBUILDER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-tests")
    monkeypatch.setenv("AIBUILDER_TOKEN", "s3cret")

    import importlib
    import app as app_module
    importlib.reload(app_module)
    client = TestClient(app_module.app)

    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_no_token_in_env_means_open_for_local_dev(monkeypatch, tmp_path):
    """Local dev convenience: if AIBUILDER_TOKEN is unset, the middleware
    passes through (no auth required). Production always sets the env var."""
    monkeypatch.setenv("AIBUILDER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-tests")
    monkeypatch.delenv("AIBUILDER_TOKEN", raising=False)

    import importlib
    import app as app_module
    importlib.reload(app_module)
    client = TestClient(app_module.app)

    sid = client.get("/api/session").json()["session_id"]
    with patch("app.run_agent_loop", return_value="ok"):
        response = client.post("/api/chat", json={"session_id": sid, "message": "hi"})
    assert response.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd aibuilder && .venv/bin/pytest tests/test_app.py -v -k token`
Expected: FAIL (the middleware doesn't exist yet).

- [ ] **Step 3: Add the middleware to `aibuilder/app.py`**

Add after the existing imports + `store = SqliteSessionStore(_DB_PATH)` line, BEFORE the route definitions:

```python
from fastapi import Request

_AUTH_TOKEN = os.environ.get("AIBUILDER_TOKEN")
_OPEN_PATHS = ("/api/health", "/static/", "/")
_OPEN_FILES = ("/govtech-barbados.png", "/favicon.ico")


@app.middleware("http")
async def require_bearer_token(request: Request, call_next):
    """Reject /api/* requests that don't carry a matching bearer token.

    Local dev: leave AIBUILDER_TOKEN unset and the middleware passes
    everything through. Production sets the env var via ECS task secrets,
    making the API surface unreachable without the right header.
    """
    path = request.url.path
    if _AUTH_TOKEN is None:
        return await call_next(request)
    if path in _OPEN_FILES or any(path.startswith(p) for p in _OPEN_PATHS):
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header != f"Bearer {_AUTH_TOKEN}":
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid Authorization header"},
        )
    return await call_next(request)
```

Also add to the imports block at the top:

```python
from fastapi.responses import JSONResponse
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd aibuilder && .venv/bin/pytest tests/test_app.py -v -k token`
Expected: 4 passed.

- [ ] **Step 5: Run full suite to confirm no regressions**

Run: `cd aibuilder && .venv/bin/pytest tests/ -v 2>&1 | tail -3`
Expected: 113 passed (was 109 + 4 new).

- [ ] **Step 6: Commit**

```bash
git add aibuilder/app.py aibuilder/tests/test_app.py
git commit -m "$(cat <<'EOF'
feat(aibuilder): bearer-token auth middleware on /api/*

Adds a FastAPI middleware that rejects /api/* requests without a
matching `Authorization: Bearer <token>` header when AIBUILDER_TOKEN
is set in the env. Local dev leaves the var unset and the middleware
passes through; production sets it via ECS task secrets injected from
SSM Parameter Store.

Open paths (no auth required):
- /api/health (ALB + CloudFront health checks)
- /static/*  (the bundled chat UI files)
- /          (the chat UI HTML itself, so the page can load before the
              user pastes their token)

Four new tests:
- 401 when token required but missing/wrong
- 200 when token matches
- /api/health stays open regardless of token
- no-token-in-env = open for local dev

Part of the GovTech sandbox hosting work
(docs/superpowers/specs/2026-06-08-aibuilder-hosting-design.md item §9).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Swap Anthropic client for AnthropicBedrock in `agent.py`

**Files:**
- Modify: `aibuilder/agent.py`
- Modify: `aibuilder/app.py` (where the client is instantiated)
- Modify: `aibuilder/tests/test_agent.py`

- [ ] **Step 1: Update `aibuilder/app.py` client instantiation**

Find the line:

```python
client = anthropic.Anthropic()
```

Replace with:

```python
client = anthropic.AnthropicBedrock(
    aws_region=os.environ.get("AWS_REGION", "us-east-1"),
)
```

Boto3-based credential chain (env vars, ~/.aws, IAM role) — no API key required. Local dev needs an active SSO session; cloud uses the ECS task role.

- [ ] **Step 2: Make the model ID configurable in `aibuilder/agent.py`**

Find:

```python
model="claude-opus-4-6",
```

Replace with a module-level constant + reference. Add near the top of `agent.py`:

```python
import os

MODEL_ID = os.environ.get("AIBUILDER_BEDROCK_MODEL", "anthropic.claude-opus-4-6-v1:0")
```

Then in `run_agent_loop`, replace the model arg with:

```python
model=MODEL_ID,
```

- [ ] **Step 3: Update the agent loop tests to mock the new client**

Open `aibuilder/tests/test_agent.py`. Find the test that constructs a fake Anthropic client (look for `anthropic.Anthropic` or `MagicMock()` used for the LLM). Update the assertions / mocks to use `anthropic.AnthropicBedrock` if any explicit class name appears.

If the existing tests just pass a `MagicMock()` to `run_agent_loop` and don't care about the class, no changes needed — the tests already work because the function signature accepts any client with `.messages.create()`.

Run the existing tests to find out:

```bash
cd aibuilder && .venv/bin/pytest tests/test_agent.py -v
```

If they pass, skip the next step.
If they fail because of `Anthropic` vs `AnthropicBedrock`, update the mock construction to use `MagicMock(spec=anthropic.AnthropicBedrock)` or just `MagicMock()`.

- [ ] **Step 4: Add a test that the model ID is configurable from env**

Append to `aibuilder/tests/test_agent.py`:

```python
def test_model_id_reads_from_env(monkeypatch):
    monkeypatch.setenv("AIBUILDER_BEDROCK_MODEL", "anthropic.claude-test-model:0")
    import importlib
    import agent as agent_module
    importlib.reload(agent_module)
    assert agent_module.MODEL_ID == "anthropic.claude-test-model:0"


def test_model_id_default_is_claude_opus_46(monkeypatch):
    monkeypatch.delenv("AIBUILDER_BEDROCK_MODEL", raising=False)
    import importlib
    import agent as agent_module
    importlib.reload(agent_module)
    assert agent_module.MODEL_ID == "anthropic.claude-opus-4-6-v1:0"
```

- [ ] **Step 5: Run tests**

Run: `cd aibuilder && .venv/bin/pytest tests/test_agent.py -v`
Expected: all pass, including 2 new tests.

Run: `cd aibuilder && .venv/bin/pytest tests/ -v 2>&1 | tail -3`
Expected: 115 passed (113 + 2 new).

- [ ] **Step 6: Commit**

```bash
git add aibuilder/agent.py aibuilder/app.py aibuilder/tests/test_agent.py
git commit -m "$(cat <<'EOF'
feat(aibuilder): swap Anthropic client for AnthropicBedrock

Local dev and cloud now both call Claude via AWS Bedrock instead of
the Anthropic API. Same code path everywhere — boto3 picks up
credentials from the standard chain (env vars / ~/.aws / IAM task
role), so local needs `aws sso login` active and cloud uses the
ECS task role's bedrock:InvokeModel permission.

Model ID moved to a module-level constant
(AIBUILDER_BEDROCK_MODEL env override) so model upgrades don't
require code changes. Default: anthropic.claude-opus-4-6-v1:0.

Drops the ANTHROPIC_API_KEY env var requirement entirely — no
parallel Anthropic-direct fallback. Strict local/cloud parity per
the hosting spec.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Frontend localStorage token prompt

**Files:**
- Modify: `aibuilder/static/index.html`

- [ ] **Step 1: Read the current chat UI JS section**

Read `aibuilder/static/index.html` — locate the `<script>` block. Find the `fetch("/api/chat", ...)` call and the `fetch("/api/session")` call.

- [ ] **Step 2: Add a token-management helper at the top of the `<script>` block**

Add immediately after the opening `<script>` tag:

```javascript
function getAuthToken() {
  let token = localStorage.getItem('aibuilder_token');
  if (!token) {
    token = prompt('Bearer token for aibuilder (one-time, stored in localStorage):');
    if (token) localStorage.setItem('aibuilder_token', token);
  }
  return token || '';
}

function authHeaders() {
  const token = getAuthToken();
  return token ? { 'Authorization': `Bearer ${token}` } : {};
}
```

- [ ] **Step 3: Wire the headers into every `/api/*` fetch call**

Find the fetch for `/api/session` — typically `fetch('/api/session')` — and change to:

```javascript
fetch('/api/session', { headers: authHeaders() })
```

Find the fetch for `/api/chat` — typically `fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: ... })` — and merge the auth headers:

```javascript
fetch('/api/chat', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', ...authHeaders() },
  body: JSON.stringify({ session_id: sessionId, message: msg }),
})
```

- [ ] **Step 4: Add a 401 handler that clears the bad token**

In the response-handling block (wherever a non-OK response is currently caught), add:

```javascript
if (response.status === 401) {
  localStorage.removeItem('aibuilder_token');
  alert('Bearer token rejected. Refresh to enter a new one.');
  return;
}
```

- [ ] **Step 5: Manual verification**

Run the existing server:

```bash
cd aibuilder && ./run.sh
```

Open http://localhost:8001 in a browser. Open dev tools → Application → Local Storage → http://localhost:8001 and confirm `aibuilder_token` appears after the first chat send.

Since `AIBUILDER_TOKEN` isn't set locally, the middleware passes everything through, so even a wrong token works — the test here is just that the prompt appears and the value persists.

- [ ] **Step 6: Commit**

```bash
git add aibuilder/static/index.html
git commit -m "$(cat <<'EOF'
feat(aibuilder): chat UI prompts for and sends bearer token

On first chat send the UI prompts for a bearer token and stores it
in localStorage; all /api/* fetches include `Authorization: Bearer
<token>`. On 401 the bad token is cleared and the user is told to
refresh.

Local dev with no AIBUILDER_TOKEN set still works — the middleware
passes through and the token value is ignored server-side. Production
sets the env var via ECS task secrets and rejects any mismatch.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Phase B — Container

### Task 4: `.dockerignore`

**Files:**
- Create: `aibuilder/.dockerignore`

- [ ] **Step 1: Create `aibuilder/.dockerignore`**

```
.venv/
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.ruff_cache/
.mypy_cache/

# Local runtime / state
data/
tmp/

# Editor / OS
.vscode/
.idea/
*.swp
.DS_Store

# Secrets
.env
.env.*
!.env.example

# Docker artifacts that shouldn't go into the image
docker-compose.yml
Dockerfile
.dockerignore

# Tests run via `make check` outside the image, no need to ship them
# (commented out — keeping them in for the CI test job)
# tests/
```

- [ ] **Step 2: Commit**

```bash
git add aibuilder/.dockerignore
git commit -m "chore(aibuilder): .dockerignore excludes venv / state / secrets"
```

---

### Task 5: `Dockerfile`

**Files:**
- Create: `aibuilder/Dockerfile`

- [ ] **Step 1: Create `aibuilder/Dockerfile`**

```dockerfile
FROM python:3.13-slim AS base

WORKDIR /aibuilder

# `git` is needed by tools.clone_repo (shells out to `git clone`).
# `ca-certificates` is needed for HTTPS git clones from GitHub.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         git \
         ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY . .

# Use a non-default port to make ALB / port-collision debugging clearer.
EXPOSE 8001

# Cloud runs the immutable image with no --reload. Local dev gets
# hot-reload via the docker-compose volume mount + a different CMD
# override (see docker-compose.yml).
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
```

- [ ] **Step 2: Build the image and verify it runs**

```bash
cd aibuilder
docker build -t aibuilder:dev .
docker run --rm -p 8001:8001 aibuilder:dev &
sleep 3
curl -sf http://127.0.0.1:8001/api/health
docker kill $(docker ps -q -f ancestor=aibuilder:dev) 2>/dev/null || true
```

Expected: `{"status":"ok"}`

- [ ] **Step 3: Verify `make check` runs inside the image**

```bash
docker run --rm -v "$(pwd):/aibuilder" aibuilder:dev make check 2>&1 | tail -3
```

Expected: 115 passed (or whatever the current baseline is).

- [ ] **Step 4: Commit**

```bash
git add aibuilder/Dockerfile
git commit -m "$(cat <<'EOF'
feat(aibuilder): Dockerfile (python:3.13-slim base, git + ca-certs)

Single-stage image, < 200 MB. Installs git (needed by
tools.clone_repo's `git clone --depth=1`) and ca-certificates
(needed for HTTPS to GitHub). Production deps + dev deps installed
in one layer so the image works for both `make check` (CI) and
`uvicorn ...` (runtime).

CMD runs uvicorn on 0.0.0.0:8001 without --reload (that's a
docker-compose override for local dev). Image is immutable in
cloud — auto-reload happens via the bind mount locally only.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `docker-compose.yml`

**Files:**
- Create: `aibuilder/docker-compose.yml`

- [ ] **Step 1: Create `aibuilder/docker-compose.yml`**

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
      - AWS_PROFILE=${AWS_PROFILE:-govtech-sandbox}
      - AWS_REGION=us-east-1
      - AIBUILDER_TOKEN=${AIBUILDER_TOKEN:-}
      - AIBUILDER_BEDROCK_MODEL=${AIBUILDER_BEDROCK_MODEL:-anthropic.claude-opus-4-6-v1:0}
    # Override the immutable image's CMD with hot-reload uvicorn for local dev
    command:
      - "python3"
      - "-m"
      - "uvicorn"
      - "app:app"
      - "--host"
      - "0.0.0.0"
      - "--port"
      - "8001"
      - "--reload"

volumes:
  aibuilder_data:
```

- [ ] **Step 2: Verify the compose file is valid**

```bash
cd aibuilder && docker compose config
```

Expected: prints the resolved YAML (no validation errors).

- [ ] **Step 3: Start the stack and smoke-test it**

```bash
cd aibuilder && docker compose up --build -d
sleep 5
curl -sf http://127.0.0.1:8001/api/health
docker compose down
```

Expected: `{"status":"ok"}`

- [ ] **Step 4: Commit**

```bash
git add aibuilder/docker-compose.yml
git commit -m "$(cat <<'EOF'
feat(aibuilder): docker-compose for local dev

Same Dockerfile, with three local-dev-only conveniences:
- Bind mount the source dir for hot-reload (uvicorn --reload)
- Named volume for SQLite session DB so it persists across `down`
- ~/.aws mounted read-only so boto3 inside the container reads the
  developer's SSO cache (Bedrock + Pricing API both work locally)

AIBUILDER_TOKEN defaults to empty so local dev needs no auth — the
middleware passes through. Set it in your .env to test the
production auth path.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Phase C — Infrastructure

> **All Phase C tasks run from `aibuilder/infra/stacks/aibuilder-hosting/`** unless otherwise stated. The user is expected to run `tofu init` once after Task 7 and `tofu plan` after each subsequent task to verify the additions.

### Task 7: Stack skeleton (providers, variables, outputs, locals, Makefile, README)

**Files:**
- Create: `aibuilder/infra/Makefile`
- Create: `aibuilder/infra/README.md`
- Create: `aibuilder/infra/stacks/aibuilder-hosting/providers.tf`
- Create: `aibuilder/infra/stacks/aibuilder-hosting/variables.tf`
- Create: `aibuilder/infra/stacks/aibuilder-hosting/outputs.tf`
- Create: `aibuilder/infra/stacks/aibuilder-hosting/locals.tf`

- [ ] **Step 1: Create `aibuilder/infra/Makefile`** (mirrors `infra/Makefile` shape)

```makefile
.PHONY: init plan apply destroy fmt validate

STACK := aibuilder-hosting
STACK_DIR := stacks/$(STACK)
AWS_PROFILE ?= govtech-sandbox

init:
	cd $(STACK_DIR) && tofu init -upgrade

plan:
	cd $(STACK_DIR) && AWS_PROFILE=$(AWS_PROFILE) tofu plan

apply:
	cd $(STACK_DIR) && AWS_PROFILE=$(AWS_PROFILE) tofu apply

destroy:
	cd $(STACK_DIR) && AWS_PROFILE=$(AWS_PROFILE) tofu destroy

fmt:
	tofu fmt -recursive

validate:
	cd $(STACK_DIR) && tofu init -backend=false -upgrade && tofu validate
```

- [ ] **Step 2: Create `aibuilder/infra/README.md`**

```markdown
# aibuilder hosting infra

OpenTofu stack that provisions the aibuilder hosting environment in
the `govtech-sandbox` AWS account (us-east-1).

## First deploy

1. `aws sso login --sso-session govtech`
2. `make init`
3. `make plan` — review
4. `make apply`
5. Enable Claude Opus 4.6 in the Bedrock console of govtech-sandbox
   (Bedrock → Model access → Manage model access → Anthropic → Claude Opus 4.6 → Request access).
6. Take the output `cloudfront_domain` and visit `https://<that>/`.
7. The chat UI will prompt you for the bearer token on first send.
   Get it: `aws ssm get-parameter --name /aibuilder/auth-token --with-decryption --query Parameter.Value --output text --profile govtech-sandbox`.

## Subsequent deploys

GitHub Actions handles them on push to `main` (see
`.github/workflows/aibuilder-deploy.yml`). Manual `make apply` is
only needed for infrastructure changes — image updates roll
automatically.

## State backend

Local state (gitignored). Single-developer / GitHub-Actions-only
applies — multi-developer applies would conflict. Migrating to an S3
backend is a follow-up (see commented block in `providers.tf`).
```

- [ ] **Step 3: Create `providers.tf`**

```hcl
terraform {
  required_version = ">= 1.8"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # When ready to share state across developers / CI, uncomment + create
  # the bucket and DynamoDB table first, then `tofu init -migrate-state`.
  # backend "s3" {
  #   bucket         = "aibuilder-tofu-state-672203047922"
  #   key            = "aibuilder-hosting/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "aibuilder-tofu-lock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}
```

- [ ] **Step 4: Create `variables.tf`**

```hcl
variable "aws_region" {
  description = "AWS region for the hosting stack."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Used as a prefix for AWS resource names + tags."
  type        = string
  default     = "aibuilder"
}

variable "env" {
  description = "Environment name (sandbox / staging / prod). Drives resource naming."
  type        = string
  default     = "sandbox"
}

variable "image_tag" {
  description = "ECR image tag the ECS task definition pins to. CI usually rotates this via `aws ecs update-service --force-new-deployment` after pushing a new image with this tag."
  type        = string
  default     = "latest"
}

variable "bedrock_model_id" {
  description = "Bedrock model ID for the AIBUILDER_BEDROCK_MODEL env var injected into the task."
  type        = string
  default     = "anthropic.claude-opus-4-6-v1:0"
}

variable "github_repo" {
  description = "GitHub repository (owner/name) allowed to assume the deploy role via OIDC."
  type        = string
  default     = "christophercorbin/infra-prototypes"
}
```

- [ ] **Step 5: Create `locals.tf`**

```hcl
locals {
  name = "${var.project}-${var.env}"

  common_tags = {
    Project   = var.project
    Env       = var.env
    ManagedBy = "OpenTofu"
    Stack     = "aibuilder-hosting"
  }
}
```

- [ ] **Step 6: Create `outputs.tf` (empty stub for now)**

```hcl
# Outputs are added by individual resource files as they land.
# After everything is up, expect:
#   - cloudfront_domain   (the d<rand>.cloudfront.net URL to visit)
#   - ecr_repository_url  (for `docker push` from CI)
#   - ecs_cluster_name    (for CI to target with update-service)
#   - ecs_service_name
#   - github_deploy_role_arn
```

- [ ] **Step 7: Initialize the stack**

```bash
cd aibuilder/infra/stacks/aibuilder-hosting && tofu init -upgrade
```

Expected: `Terraform has been successfully initialized!`

- [ ] **Step 8: Validate**

```bash
tofu validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 9: Update `.gitignore` to exclude tofu state from this stack**

The existing root `.gitignore` already excludes `*.tfstate` and `.terraform/` — no change needed. Verify with:

```bash
git check-ignore aibuilder/infra/stacks/aibuilder-hosting/.terraform/ 2>/dev/null && echo "ignored" || echo "NOT ignored"
```

Expected: `ignored`

- [ ] **Step 10: Commit**

```bash
cd /Users/christophercorbin/INFRA\ prototypes
git add aibuilder/infra/
git commit -m "$(cat <<'EOF'
scaffold(aibuilder): infra/ OpenTofu stack skeleton

New stack at aibuilder/infra/stacks/aibuilder-hosting/. Sibling to
infra/ (the deploy-agent stack), same conventions (Makefile per
project, file-level decomposition by resource type, local state
for MVP with S3 backend block commented out).

Resources are added in subsequent commits (networking, ECR, EFS,
IAM, ALB, ECS, CloudFront, OIDC). This commit is just the skeleton:
providers, variables, outputs stub, locals, README, Makefile.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Networking — security groups + VPC data sources

**Files:**
- Create: `aibuilder/infra/stacks/aibuilder-hosting/networking.tf`

- [ ] **Step 1: Create `networking.tf`**

```hcl
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default_public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "ALB ingress from CloudFront edge IPs only"
  vpc_id      = data.aws_vpc.default.id

  # CloudFront edge IPs are dynamic. The simplest correct rule for MVP is
  # to allow 0.0.0.0/0 on 80 — only the CloudFront distribution knows
  # the ALB DNS name (it's not in DNS) and the chat UI route is auth-
  # gated. Hardening to the AWS-managed CloudFront prefix list
  # (com.amazonaws.global.cloudfront.origin-facing) is a follow-up.
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-alb" }
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "Fargate task: only accepts traffic from the ALB SG"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port       = 8001
    to_port         = 8001
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-task" }
}

resource "aws_security_group" "efs" {
  name        = "${local.name}-efs"
  description = "EFS: only accepts NFS (2049) from the Fargate task SG"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-efs" }
}
```

- [ ] **Step 2: Plan**

```bash
cd aibuilder/infra/stacks/aibuilder-hosting && AWS_PROFILE=govtech-sandbox tofu plan
```

Expected: plan to create 3 security groups + 2 data source reads. No errors.

- [ ] **Step 3: Commit**

```bash
git add aibuilder/infra/stacks/aibuilder-hosting/networking.tf
git commit -m "feat(aibuilder/infra): default-VPC + 3 security groups (alb, task, efs)"
```

---

### Task 9: ECR repository

**Files:**
- Create: `aibuilder/infra/stacks/aibuilder-hosting/ecr.tf`

- [ ] **Step 1: Create `ecr.tf`**

```hcl
resource "aws_ecr_repository" "aibuilder" {
  name                 = local.name
  image_tag_mutability = "MUTABLE" # `latest` tag gets reused on each deploy

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "aibuilder" {
  repository = aws_ecr_repository.aibuilder.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 sha-tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Delete untagged after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      }
    ]
  })
}

output "ecr_repository_url" {
  value       = aws_ecr_repository.aibuilder.repository_url
  description = "ECR URL to push images to (used by GitHub Actions)"
}
```

- [ ] **Step 2: Plan**

```bash
tofu plan
```

Expected: 2 resources to add (repository + lifecycle policy).

- [ ] **Step 3: Commit**

```bash
git add aibuilder/infra/stacks/aibuilder-hosting/ecr.tf
git commit -m "feat(aibuilder/infra): ECR repository with lifecycle policy"
```

---

### Task 10: EFS file system + access point

**Files:**
- Create: `aibuilder/infra/stacks/aibuilder-hosting/efs.tf`

- [ ] **Step 1: Create `efs.tf`**

```hcl
resource "aws_efs_file_system" "sessions" {
  creation_token   = "${local.name}-sessions"
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  tags = { Name = "${local.name}-sessions" }
}

resource "aws_efs_mount_target" "sessions" {
  for_each = toset(data.aws_subnets.default_public.ids)

  file_system_id  = aws_efs_file_system.sessions.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

# Access point pins the container's view of the file system to a
# specific subdirectory + UID/GID. The Fargate task mounts the
# access point at /aibuilder/data inside the container.
resource "aws_efs_access_point" "sessions" {
  file_system_id = aws_efs_file_system.sessions.id

  posix_user {
    uid = 0
    gid = 0
  }

  root_directory {
    path = "/aibuilder-data"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "755"
    }
  }
}
```

- [ ] **Step 2: Plan**

```bash
tofu plan
```

Expected: 1 file system + N mount targets (one per default subnet) + 1 access point.

- [ ] **Step 3: Commit**

```bash
git add aibuilder/infra/stacks/aibuilder-hosting/efs.tf
git commit -m "feat(aibuilder/infra): EFS for SQLite session persistence"
```

---

### Task 11: SSM parameter + CloudWatch log group

**Files:**
- Create: `aibuilder/infra/stacks/aibuilder-hosting/ssm.tf`
- Create: `aibuilder/infra/stacks/aibuilder-hosting/cloudwatch.tf`

- [ ] **Step 1: Create `ssm.tf`**

```hcl
# We don't generate the token in OpenTofu — that would put it in the
# state file. After first apply, manually set:
#   aws ssm put-parameter \
#     --name /aibuilder/auth-token \
#     --type SecureString \
#     --value "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
#     --overwrite \
#     --profile govtech-sandbox
# The lifecycle ignore_changes block means OpenTofu won't try to
# overwrite a value set out-of-band.
resource "aws_ssm_parameter" "auth_token" {
  name        = "/aibuilder/auth-token"
  description = "Bearer token required for /api/* requests; injected as AIBUILDER_TOKEN"
  type        = "SecureString"
  value       = "PLACEHOLDER_OVERWRITE_VIA_CLI"

  lifecycle {
    ignore_changes = [value]
  }

  tags = { Name = "${local.name}-auth-token" }
}
```

- [ ] **Step 2: Create `cloudwatch.tf`**

```hcl
resource "aws_cloudwatch_log_group" "task" {
  name              = "/ecs/${local.name}"
  retention_in_days = 7

  tags = { Name = "${local.name}-task-logs" }
}
```

- [ ] **Step 3: Plan**

```bash
tofu plan
```

Expected: 2 resources to add (1 SSM param + 1 log group).

- [ ] **Step 4: Commit**

```bash
git add aibuilder/infra/stacks/aibuilder-hosting/ssm.tf aibuilder/infra/stacks/aibuilder-hosting/cloudwatch.tf
git commit -m "feat(aibuilder/infra): SSM auth-token parameter + CloudWatch log group"
```

---

### Task 12: IAM roles (task execution + task)

**Files:**
- Create: `aibuilder/infra/stacks/aibuilder-hosting/iam.tf`

- [ ] **Step 1: Create `iam.tf`**

```hcl
# Task execution role: used by ECS itself (NOT the running container)
# to pull the image from ECR, write logs, and resolve secrets from SSM.
resource "aws_iam_role" "task_execution" {
  name = "${local.name}-task-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Lets the task execution role read the bearer-token SSM parameter
# at task launch (to inject as the AIBUILDER_TOKEN env var).
data "aws_caller_identity" "current" {}

resource "aws_iam_role_policy" "task_execution_ssm" {
  name = "${local.name}-task-execution-ssm"
  role = aws_iam_role.task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["ssm:GetParameters"]
      Resource = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${aws_ssm_parameter.auth_token.name}"
      ]
    }]
  })
}

# Task role: used by the running container's code (boto3 calls from
# inside the app). Gets Bedrock + Pricing API + EFS access.
resource "aws_iam_role" "task" {
  name = "${local.name}-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task_bedrock" {
  name = "${local.name}-task-bedrock"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
      ]
      # us-east-1 + us-west-2 are common Bedrock regions. Scope here
      # to us-east-1 specifically since that's our region.
      Resource = [
        "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.bedrock_model_id}",
      ]
    }]
  })
}

resource "aws_iam_role_policy" "task_pricing" {
  name = "${local.name}-task-pricing"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "pricing:GetProducts",
        "pricing:GetAttributeValues",
        "pricing:DescribeServices",
      ]
      # The Pricing API doesn't support resource-level ARN scoping.
      Resource = "*"
    }]
  })
}

resource "aws_iam_role_policy" "task_efs" {
  name = "${local.name}-task-efs"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "elasticfilesystem:ClientMount",
        "elasticfilesystem:ClientWrite",
      ]
      Resource = [aws_efs_file_system.sessions.arn]
    }]
  })
}
```

- [ ] **Step 2: Plan**

```bash
tofu plan
```

Expected: 2 IAM roles + 1 managed policy attachment + 4 inline policies.

- [ ] **Step 3: Commit**

```bash
git add aibuilder/infra/stacks/aibuilder-hosting/iam.tf
git commit -m "feat(aibuilder/infra): IAM roles for ECS task execution + task runtime"
```

---

### Task 13: ALB + listener + target group

**Files:**
- Create: `aibuilder/infra/stacks/aibuilder-hosting/alb.tf`

- [ ] **Step 1: Create `alb.tf`**

```hcl
resource "aws_lb" "aibuilder" {
  name               = local.name
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default_public.ids

  tags = { Name = local.name }
}

resource "aws_lb_target_group" "aibuilder" {
  name        = local.name
  port        = 8001
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.default.id

  health_check {
    path                = "/api/health"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    matcher             = "200"
  }

  # Fargate replaces tasks during deploys; let the existing connections
  # finish quickly rather than holding the deploy.
  deregistration_delay = 30
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.aibuilder.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.aibuilder.arn
  }
}

output "alb_dns_name" {
  value       = aws_lb.aibuilder.dns_name
  description = "ALB internal-facing hostname; CloudFront uses this as its origin"
}
```

- [ ] **Step 2: Plan**

```bash
tofu plan
```

Expected: 1 ALB + 1 target group + 1 listener + 1 output. No errors.

- [ ] **Step 3: Commit**

```bash
git add aibuilder/infra/stacks/aibuilder-hosting/alb.tf
git commit -m "feat(aibuilder/infra): ALB + listener + target group (HTTP on 80)"
```

---

### Task 14: ECS cluster + task definition + service

**Files:**
- Create: `aibuilder/infra/stacks/aibuilder-hosting/ecs.tf`

- [ ] **Step 1: Create `ecs.tf`**

```hcl
resource "aws_ecs_cluster" "aibuilder" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "disabled" # cost-saving; enable later if observability calls for it
  }
}

resource "aws_ecs_task_definition" "aibuilder" {
  family                   = local.name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"  # 0.25 vCPU
  memory                   = "512"  # 0.5 GB
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "aibuilder"
    image     = "${aws_ecr_repository.aibuilder.repository_url}:${var.image_tag}"
    essential = true

    portMappings = [{
      containerPort = 8001
      protocol      = "tcp"
    }]

    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "AIBUILDER_BEDROCK_MODEL", value = var.bedrock_model_id },
      { name = "AIBUILDER_DB", value = "/aibuilder/data/sessions.db" },
    ]

    secrets = [
      {
        name      = "AIBUILDER_TOKEN"
        valueFrom = aws_ssm_parameter.auth_token.arn
      }
    ]

    mountPoints = [{
      sourceVolume  = "sessions"
      containerPath = "/aibuilder/data"
      readOnly      = false
    }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.task.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "aibuilder"
      }
    }
  }])

  volume {
    name = "sessions"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.sessions.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.sessions.id
        iam             = "ENABLED"
      }
    }
  }
}

resource "aws_ecs_service" "aibuilder" {
  name            = local.name
  cluster         = aws_ecs_cluster.aibuilder.id
  task_definition = aws_ecs_task_definition.aibuilder.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default_public.ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true # required for tasks in public subnets to pull from ECR
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.aibuilder.arn
    container_name   = "aibuilder"
    container_port   = 8001
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200

  depends_on = [aws_lb_listener.http]
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.aibuilder.name
}

output "ecs_service_name" {
  value = aws_ecs_service.aibuilder.name
}
```

- [ ] **Step 2: Plan**

```bash
tofu plan
```

Expected: 1 cluster + 1 task definition + 1 service + 2 outputs.

- [ ] **Step 3: Commit**

```bash
git add aibuilder/infra/stacks/aibuilder-hosting/ecs.tf
git commit -m "feat(aibuilder/infra): ECS cluster + task definition + service"
```

---

### Task 15: CloudFront distribution

**Files:**
- Create: `aibuilder/infra/stacks/aibuilder-hosting/cloudfront.tf`

- [ ] **Step 1: Create `cloudfront.tf`**

```hcl
resource "aws_cloudfront_distribution" "aibuilder" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "${local.name} — HTTPS front door for the ALB"
  price_class     = "PriceClass_100" # US + EU edges only — cheaper for a small audience

  origin {
    domain_name = aws_lb.aibuilder.dns_name
    origin_id   = "alb"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only" # CloudFront → ALB is HTTP inside AWS
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = "alb"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]

    # No caching of dynamic content. The chat is fully personalised.
    min_ttl     = 0
    default_ttl = 0
    max_ttl     = 0

    # Forward the headers the FastAPI middleware needs.
    forwarded_values {
      query_string = true
      headers      = ["Authorization", "Host", "Content-Type"]

      cookies {
        forward = "none"
      }
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true # *.cloudfront.net cert, free
  }
}

output "cloudfront_domain" {
  value       = "https://${aws_cloudfront_distribution.aibuilder.domain_name}"
  description = "Public HTTPS URL for aibuilder. Visit this in a browser."
}
```

- [ ] **Step 2: Plan**

```bash
tofu plan
```

Expected: 1 CloudFront distribution + 1 output. NOTE: CloudFront resource creates and propagates take ~5-10 min during apply.

- [ ] **Step 3: Commit**

```bash
git add aibuilder/infra/stacks/aibuilder-hosting/cloudfront.tf
git commit -m "feat(aibuilder/infra): CloudFront distribution in front of the ALB"
```

---

### Task 16: GitHub OIDC IdP + deploy role

**Files:**
- Create: `aibuilder/infra/stacks/aibuilder-hosting/oidc.tf`

- [ ] **Step 1: Create `oidc.tf`**

```hcl
# Conditionally create the GitHub OIDC IdP — if it already exists in the
# account (because some other workflow set it up), the data source picks
# it up and we skip creation. This makes the stack safe to apply in any
# account.
data "aws_iam_openid_connect_providers" "github" {}

locals {
  github_oidc_url       = "https://token.actions.githubusercontent.com"
  github_oidc_provider_arn = length([
    for p in data.aws_iam_openid_connect_providers.github.arns : p
    if can(regex("token.actions.githubusercontent.com", p))
  ]) > 0 ? [
    for p in data.aws_iam_openid_connect_providers.github.arns : p
    if can(regex("token.actions.githubusercontent.com", p))
  ][0] : aws_iam_openid_connect_provider.github[0].arn
}

resource "aws_iam_openid_connect_provider" "github" {
  count = length([
    for p in data.aws_iam_openid_connect_providers.github.arns : p
    if can(regex("token.actions.githubusercontent.com", p))
  ]) > 0 ? 0 : 1

  url             = local.github_oidc_url
  client_id_list  = ["sts.amazonaws.com"]
  # GitHub's OIDC thumbprint — AWS docs publish this list; current as of 2024.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_deploy" {
  name = "${local.name}-github-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # Trust only main branch + PRs in the configured repo.
          "token.actions.githubusercontent.com:sub" = [
            "repo:${var.github_repo}:ref:refs/heads/main",
            "repo:${var.github_repo}:pull_request",
          ]
        }
      }
    }]
  })
}

# Deploy role gets exactly what GitHub Actions needs: ECR push +
# describe + ECS update-service + describe-services.
resource "aws_iam_role_policy" "github_deploy" {
  name = "${local.name}-github-deploy"
  role = aws_iam_role.github_deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeRepositories",
          "ecr:DescribeImages",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ]
        Resource = aws_ecr_repository.aibuilder.arn
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:DescribeServices",
          "ecs:UpdateService",
        ]
        Resource = aws_ecs_service.aibuilder.id
      },
      {
        Effect = "Allow"
        Action = ["ecs:DescribeTaskDefinition"]
        Resource = "*"
      },
    ]
  })
}

output "github_deploy_role_arn" {
  value       = aws_iam_role.github_deploy.arn
  description = "Role ARN GitHub Actions assumes via OIDC; goes in .github/workflows/aibuilder-deploy.yml"
}
```

- [ ] **Step 2: Plan**

```bash
tofu plan
```

Expected: 1 IdP (only if it doesn't exist) + 1 IAM role + 1 inline policy + 1 output.

- [ ] **Step 3: Commit**

```bash
git add aibuilder/infra/stacks/aibuilder-hosting/oidc.tf
git commit -m "feat(aibuilder/infra): GitHub OIDC IdP + deploy role"
```

---

## Phase D — CI/CD

### Task 17: GitHub Actions deploy workflow

**Files:**
- Create: `.github/workflows/aibuilder-deploy.yml`

- [ ] **Step 1: Create the workflow**

Replace `<ROLE_ARN_PLACEHOLDER>` after the first `tofu apply` with the value of the `github_deploy_role_arn` output. For now write the file with the placeholder; the deploy task (Task 19) replaces it.

```yaml
name: aibuilder deploy

on:
  push:
    branches: [main]
    paths:
      - 'aibuilder/**'
      - '.github/workflows/aibuilder-deploy.yml'
  pull_request:
    paths:
      - 'aibuilder/**'

permissions:
  id-token: write   # required to request the OIDC token
  contents: read

env:
  AWS_REGION: us-east-1
  ECR_REPO_NAME: aibuilder-sandbox
  ECS_CLUSTER: aibuilder-sandbox
  ECS_SERVICE: aibuilder-sandbox

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build image
        run: docker build -t aibuilder:test ./aibuilder
      - name: Run `make check` inside the image
        run: docker run --rm -v "${{ github.workspace }}/aibuilder:/aibuilder" aibuilder:test make check

  deploy:
    needs: test
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials via OIDC
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: <ROLE_ARN_PLACEHOLDER>
          aws-region: ${{ env.AWS_REGION }}

      - name: Login to ECR
        id: ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build + push image
        env:
          ECR_REGISTRY: ${{ steps.ecr.outputs.registry }}
          SHA_TAG: sha-${{ github.sha }}
        run: |
          IMG_LATEST=$ECR_REGISTRY/$ECR_REPO_NAME:latest
          IMG_SHA=$ECR_REGISTRY/$ECR_REPO_NAME:${SHA_TAG:0:11}
          docker build -t $IMG_LATEST -t $IMG_SHA ./aibuilder
          docker push $IMG_LATEST
          docker push $IMG_SHA

      - name: Roll the ECS service
        run: |
          aws ecs update-service \
            --cluster $ECS_CLUSTER \
            --service $ECS_SERVICE \
            --force-new-deployment \
            --region $AWS_REGION

      - name: Wait for stable
        run: |
          aws ecs wait services-stable \
            --cluster $ECS_CLUSTER \
            --services $ECS_SERVICE \
            --region $AWS_REGION
```

- [ ] **Step 2: Lint the YAML** (best effort — `actionlint` is optional)

```bash
yamllint .github/workflows/aibuilder-deploy.yml 2>/dev/null || echo "yamllint not installed, skipping"
```

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/aibuilder-deploy.yml
git commit -m "$(cat <<'EOF'
feat(ci): GitHub Actions workflow to build, test, push, deploy aibuilder

Two jobs:
- test (always on push + PR): build the Docker image, run `make check`
  inside it. Fail-closed; PRs that break tests can't merge.
- deploy (push to main only, after test passes): assume the
  aibuilder-sandbox-github-deploy role via OIDC, push the image to
  ECR with `latest` + `sha-<7chr>` tags, force a new ECS deployment,
  wait for the service to stabilise.

ROLE ARN is left as a placeholder. First infrastructure deploy
(Task 19) outputs github_deploy_role_arn — paste it into the
workflow then push to trigger the first push-to-deploy.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Phase E — First deploy + smoke test

### Task 18: Outputs wiring (export everything CI needs)

**Files:**
- Modify: `aibuilder/infra/stacks/aibuilder-hosting/outputs.tf`

- [ ] **Step 1: Replace the stub `outputs.tf` with the real exports**

The outputs were added inline in earlier tasks (ecr.tf exports `ecr_repository_url`, alb.tf exports `alb_dns_name`, ecs.tf exports cluster + service names, cloudfront.tf exports `cloudfront_domain`, oidc.tf exports `github_deploy_role_arn`). Verify they're all present:

```bash
cd aibuilder/infra/stacks/aibuilder-hosting && grep -h '^output' *.tf
```

Expected: 6 output declarations.

- [ ] **Step 2: Delete the stub `outputs.tf` (its content was just a comment)**

```bash
rm aibuilder/infra/stacks/aibuilder-hosting/outputs.tf
```

- [ ] **Step 3: Re-validate**

```bash
tofu validate
```

Expected: success.

- [ ] **Step 4: Commit**

```bash
git add aibuilder/infra/stacks/aibuilder-hosting/outputs.tf
git commit -m "chore(aibuilder/infra): remove placeholder outputs.tf (live outputs in resource files)"
```

---

### Task 19: First deploy — manual bootstrap

This task is operational. The implementer runs commands locally; nothing is committed in this task (beyond a possible CLAUDE.md update in Task 20).

- [ ] **Step 1: Authenticate AWS SSO**

```bash
aws sso login --sso-session govtech
```

- [ ] **Step 2: First `tofu apply`**

```bash
cd aibuilder/infra/stacks/aibuilder-hosting
AWS_PROFILE=govtech-sandbox tofu apply
```

Type `yes` when prompted. Expect ~30 resources to create. CloudFront propagation takes ~5–10 min — `apply` will wait.

When apply completes, capture the outputs:

```bash
tofu output -json
```

Note these values:
- `cloudfront_domain` (the URL to visit)
- `github_deploy_role_arn` (paste into the GH Actions workflow)

- [ ] **Step 3: Set the real bearer token in SSM**

```bash
TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
aws ssm put-parameter \
  --name /aibuilder/auth-token \
  --type SecureString \
  --value "$TOKEN" \
  --overwrite \
  --profile govtech-sandbox
echo "Bearer token: $TOKEN  (save this somewhere — you'll paste it into the chat UI)"
```

- [ ] **Step 4: Enable Claude Opus 4.6 in the govtech-sandbox Bedrock console**

This is a manual one-time step in the AWS console:

1. Sign in to the AWS console as the `InfrastructureAdmin` role in `govtech-sandbox`.
2. Open Bedrock → Model access → Manage model access.
3. Find "Anthropic Claude Opus 4.6" → Request access.
4. Wait for the request to be approved (usually immediate; can take up to an hour for some models).

- [ ] **Step 5: Update the GitHub workflow with the real role ARN**

```bash
ROLE_ARN=$(cd aibuilder/infra/stacks/aibuilder-hosting && tofu output -raw github_deploy_role_arn)
sed -i.bak "s|<ROLE_ARN_PLACEHOLDER>|$ROLE_ARN|" .github/workflows/aibuilder-deploy.yml
rm .github/workflows/aibuilder-deploy.yml.bak
git add .github/workflows/aibuilder-deploy.yml
git commit -m "ci(aibuilder): wire the real OIDC role ARN into the workflow"
git push
```

The push to main triggers the first end-to-end CI run: build → push to ECR → roll the ECS service.

- [ ] **Step 6: Watch the deploy**

In the GitHub UI: Actions → "aibuilder deploy" → newest run. Wait for "Wait for stable" to succeed.

If the test job fails or the deploy job fails before reaching ECS, fix and re-push. Once `aws ecs wait services-stable` returns, the service has 1 running task with the new image.

---

### Task 20: End-to-end smoke test

- [ ] **Step 1: Hit the CloudFront URL**

```bash
DOMAIN=$(cd aibuilder/infra/stacks/aibuilder-hosting && tofu output -raw cloudfront_domain)
echo "Visit: $DOMAIN"
```

Open the URL in a browser. Expect the chat UI to load.

- [ ] **Step 2: Test the bearer-token gate**

Open the browser dev tools → Console. Run:

```javascript
fetch('/api/session').then(r => console.log(r.status))
```

Expected: `401`. (No Authorization header → middleware rejects.)

- [ ] **Step 3: Paste the bearer token and chat**

In the chat UI, send a message. When prompted, paste the token from Task 19 Step 3. Expected:

- Token stored in `localStorage`.
- First chat round-trip completes (Bedrock invoked, response rendered).
- Subsequent messages don't re-prompt.

If you get a 401 from Bedrock specifically (visible in the response details), Claude Opus 4.6 is probably not enabled — revisit Task 19 Step 4.

- [ ] **Step 4: Drop a real GitHub URL into the chat**

Use the same test repo from earlier sessions: `https://github.com/govtech-bb/st-thomas-sign-in`. Expect:
- `clone_repo` works (git is installed in the image).
- `analyze_repo` classifies it as `fullstack_with_db` with Next.js SSR + Supabase.
- `recommend_architecture` → `nextjs_amplify_hosting`.
- `estimate_cost` → ~$1/mo for Amplify (uses live S3 pricing if Bedrock task role works; verify `is_fallback` in the JSON response).

- [ ] **Step 5: Verify CloudWatch logs**

```bash
aws logs tail /ecs/aibuilder-sandbox --since 10m --profile govtech-sandbox --follow
```

Expected: see uvicorn startup, request logs, agent loop output.

`Ctrl+C` when done.

- [ ] **Step 6: Document the live URL in CLAUDE.md + update test count**

Append to the "aibuilder" section of `CLAUDE.md`:

```markdown
### Hosted instance

- URL: see `tofu output -raw cloudfront_domain` in `aibuilder/infra/stacks/aibuilder-hosting/`
- Bearer token: `aws ssm get-parameter --name /aibuilder/auth-token --with-decryption --query Parameter.Value --output text --profile govtech-sandbox`
- Logs: `aws logs tail /ecs/aibuilder-sandbox --follow --profile govtech-sandbox`
- Deploy: push to `main` triggers `.github/workflows/aibuilder-deploy.yml`
- Infra: `aibuilder/infra/stacks/aibuilder-hosting/`
```

And update the test count baseline:

```markdown
- `cd aibuilder && make check` → **115 tests passing**.
```

(Bumped from 107 to 115: +4 token middleware tests + +2 model ID tests + the rest unchanged. Adjust if your final count differs.)

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(claude.md): aibuilder is now hosted in govtech-sandbox

CloudFront URL, bearer-token retrieval, log-tail command, and deploy
workflow link captured under the "aibuilder" section. Test count
baseline bumped 107 -> 115 (token middleware + model ID env tests).

aibuilder is now self-hosted in the GovTech sandbox account, billing
through GovTech AWS, LLM running on Bedrock per the compliance
requirement. Sub-project B MVP complete; follow-up B2 (custom domain)
+ B3 (Cognito/SSO) + others are explicit non-goals tracked in the
hosting design spec.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
git push
```

---

## Self-review checklist (done by plan author)

- [x] **Spec coverage**: every component in the spec (Dockerfile, docker-compose, ECR, ECS, ALB, CloudFront, EFS, IAM, SSM, CloudWatch, Bedrock client swap, bearer-token middleware, frontend prompt, CI/CD, first deploy) has at least one task.
- [x] **Placeholder scan**: searched for "TBD", "TODO", "implement appropriate", "add error handling" — only `<ROLE_ARN_PLACEHOLDER>` remains, and it's intentional (filled at deploy time in Task 19 Step 5).
- [x] **Type consistency**: service names, resource names, environment variables match across tasks (`local.name = "aibuilder-sandbox"`, env var is `AIBUILDER_TOKEN` everywhere, etc.).
- [x] **Frequent commits**: 19 of 20 tasks end in a commit; Task 19 is operational (no commit) but produces a commit in its Step 5.
- [x] **Test discipline**: app-code tasks (1, 2, 3) are TDD with explicit failing-test → impl → passing-test → commit; infra tasks use `tofu plan` as the verification step.
- [x] **Each task is bite-sized**: longest task is 14 (ECS, ~80 lines of HCL in one step) — defensible because the resources are tightly coupled, splitting would force inter-task references that obscure the read.
