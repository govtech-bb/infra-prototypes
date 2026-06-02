# aibuilder — Phase 1: repo analysis & AWS cost estimate

**Status:** Draft
**Date:** 2026-06-02
**Owner:** ChristopherN.Corbin@govtech.bb

## Problem

Someone has a GitHub repo and wants to run it on AWS. They don't know what AWS services they need or what it will cost. Today the only way to get that answer is to ask a human cloud engineer (slow, doesn't scale) or read a lot of AWS documentation (intimidating, error-prone).

We already have `deploy-agent/`, which deploys static sites end-to-end. The goal of `aibuilder/` is broader: a chat bot that can look at *any* repo and tell the user (1) what's in it, (2) what AWS services it would need, (3) what those services would cost per month at low traffic. Actual deployment is out of scope for this phase — analysis and cost only.

## Goals

1. User pastes a public GitHub URL into a chat.
2. Bot clones the repo and produces a plain-language summary of what the app is and how it works.
3. User confirms or corrects the summary (the **validation step** — keeps the bot honest before it makes recommendations).
4. Bot proposes a concrete AWS architecture (named services, with reasoning).
5. Bot returns a monthly cost estimate with a per-service breakdown, sourced from the AWS Pricing API.

## Non-goals (this phase)

- Deploying anything. No `tofu apply`, no resource creation.
- Private GitHub repos / GitHub auth.
- Multi-architecture comparison ("what about Lambda instead?"). Possible follow-up.
- Security review, Well-Architected review, compliance checks.
- Traffic-tier modeling beyond a single "low-traffic prototype" baseline.
- IaC code generation. The output is a recommendation, not Terraform.

## High-level design

A new sibling app at `aibuilder/`, structured like `deploy-agent/`:

```
aibuilder/
├── app.py                # FastAPI app + chat endpoints
├── agent.py              # Claude tool-use loop + system prompt
├── sessions.py           # SQLite session store
├── tools.py              # The four tools (clone/analyze/recommend/estimate)
├── analyzer.py           # Pure-Python repo classifier
├── patterns.py           # AWS pattern catalog (the "brain")
├── pricing.py            # AWS Pricing API client + cost model
├── static/index.html     # Single-file chat UI
├── tests/
│   ├── fixtures/         # Sample repos (static, node-api, dockerized, fullstack-db)
│   ├── test_analyzer.py
│   ├── test_patterns.py
│   ├── test_pricing.py
│   └── test_tools.py
├── data/                 # SQLite db lives here (gitignored)
├── tmp/repos/            # cloned repos (gitignored)
├── Makefile
├── pyproject.toml
├── requirements.txt
├── run.sh
└── .env.example
```

The agent loop, session store, system-prompt-driven workflow, `{summary, details}` error contract, and run-script ergonomics are direct adaptations of the patterns already proven in `deploy-agent/`. We are deliberately **not** sharing code between the two apps in this phase — duplication is cheaper than a premature abstraction, and the two apps are likely to diverge as `aibuilder` grows.

## Components

### 1. Repo cloner (`tools.clone_repo`)

**Signature:** `clone_repo(github_url: str) -> dict`

Steps:
1. Validate the URL is a `https://github.com/<owner>/<repo>(.git)?` form. Reject anything else with a friendly summary.
2. Shallow-clone (`git clone --depth=1`) into `tmp/repos/<session_id>/<repo_name>/`.
3. Enforce guards:
   - **Size guard:** total repo size on disk ≤ 500 MB. If exceeded, delete the clone and return an error.
   - **File-count guard:** ≤ 5,000 files. Same handling.
4. Return `{ "path": "<abs path>", "repo_name": "...", "file_count": N, "size_mb": N }`.

Failure modes (all returned as `{summary, details}` dicts, never raised):
- Invalid URL → "That doesn't look like a GitHub repo URL. Try `https://github.com/<owner>/<repo>`."
- 404 / private → "I can only see public repos right now."
- Network / git failure → "Couldn't reach GitHub. Try again in a moment."
- Size or file-count exceeded → "This repo is too large for me to scan in one go. Point me at the subfolder for the app you want to deploy."

The clone directory is keyed on `session_id` so concurrent sessions don't collide and so cleanup is straightforward.

### 2. Repo analyzer (`analyzer.analyze_repo`, exposed via `tools.analyze_repo`)

**Signature:** `analyze_repo(path: str) -> RepoProfile`

Pure Python, no LLM call. Walks the cloned directory and produces a structured profile:

```python
@dataclass
class RepoProfile:
    app_type: str          # one of the pattern keys (see catalog), or "unknown"
    languages: list[str]   # ["python", "javascript"], detected from file extensions
    frameworks: list[str]  # ["fastapi", "react"], detected from manifest contents
    has_dockerfile: bool
    has_compose: bool
    has_database_hints: bool  # e.g. references to postgres/mysql in deps or env files
    entry_points: list[str]   # main.py, server.js, index.html, etc.
    build_command: str | None  # "npm run build" if package.json scripts has "build"
    summary: str               # human-readable 2-3 sentence description
```

The analyzer classifies by walking a decision tree of file presence + manifest contents:

| Detection                                                   | `app_type`          |
| ----------------------------------------------------------- | ------------------- |
| `index.html` at root, no backend manifests                  | `static_site`       |
| React/Vue/Svelte/Next/etc. detected + an API route directory | `spa_with_api`      |
| `package.json` with express/fastify/koa/hapi, no frontend   | `node_api`          |
| `requirements.txt`/`pyproject.toml` with FastAPI/Flask/Django, no frontend | `python_api` |
| `Dockerfile` + web server detection, no native frontend dir | `dockerized_web`    |
| Both frontend and backend present, DB hints present         | `fullstack_with_db` |
| Cron / scheduled / queue worker hints, no HTTP server       | `worker`            |
| Anything else                                               | `unknown`           |

`summary` is generated by the analyzer (templated from the detected facts), not by the LLM. The agent's job is to *present* the summary, not to *invent* it. This is what makes the validation step trustworthy.

Database hints (`has_database_hints = True`) come from string-matching across `requirements.txt`, `package.json`, `docker-compose.yml`, and any `.env*` files for tokens like `postgres`, `mysql`, `mongodb`, `redis`, `DATABASE_URL`, `DB_HOST`.

### 3. Architecture recommender (`patterns.recommend`, exposed via `tools.recommend_architecture`)

**Signature:** `recommend_architecture(profile: RepoProfile) -> Architecture`

Pure function. Given a profile, returns:

```python
@dataclass
class ArchitectureService:
    aws_service: str       # e.g. "S3", "CloudFront", "Lambda"
    purpose: str           # one sentence: why this service for this app
    sizing: dict           # input to pricing.py — e.g. {"instance_size": "0.25 vCPU, 0.5 GB"}

@dataclass
class Architecture:
    pattern: str                       # the matched pattern key
    services: list[ArchitectureService]
    notes: list[str]                   # any caveats (e.g. "ACM cert must be in us-east-1")
```

The pattern catalog lives in `aibuilder/patterns.py` as a dict, not in the system prompt. This makes it testable and gives us one place to add a new pattern.

Pattern → AWS shape (initial catalog):

| Pattern             | Services                                                                                |
| ------------------- | --------------------------------------------------------------------------------------- |
| `static_site`       | S3 (private) + CloudFront                                                               |
| `spa_with_api`      | S3 + CloudFront + API Gateway (HTTP) + Lambda                                           |
| `node_api`          | App Runner (default) **OR** Lambda + API Gateway (alternative, for stateless workloads) |
| `python_api`        | App Runner (default) **OR** Lambda + API Gateway via Mangum (alternative)               |
| `dockerized_web`    | App Runner (default) — small, autoscaling, no cluster mgmt                              |
| `fullstack_with_db` | App Runner (web) + RDS Postgres `db.t4g.micro` (or Aurora Serverless v2 min ACU)        |
| `worker`            | EventBridge Scheduler + Lambda (short jobs) **OR** ECS Fargate scheduled (long jobs)    |
| `unknown`           | No recommendation; agent asks the user to describe the app                              |

Where two options exist, v1 picks one default and mentions the alternative in `notes`. ("Default: App Runner. If you'd rather pay-per-request, ask me about Lambda.") The catalog stores the default and the alternative; the agent surfaces both.

### 4. Cost estimator (`pricing.estimate`, exposed via `tools.estimate_cost`)

**Signature:** `estimate_cost(architecture: Architecture) -> CostEstimate`

Hits the AWS Pricing API directly (via `boto3.client("pricing", region_name="us-east-1")` — the Pricing endpoint lives in `us-east-1` regardless of where the resources will run).

For each `ArchitectureService`:
1. Look up unit prices for the service in `us-east-1` (the prototype default).
2. Apply the "low-traffic prototype" baseline assumptions (see below).
3. Produce a monthly $ figure with a one-line breakdown.

**Low-traffic prototype baseline:**

| Dimension                  | Assumption                                                          |
| -------------------------- | ------------------------------------------------------------------- |
| Requests per month         | 100,000                                                             |
| Average request size       | 10 KB request / 50 KB response                                      |
| Total data out (CloudFront)| 5 GB/month                                                          |
| Lambda execution time      | 200 ms avg, 256 MB memory                                           |
| App Runner sizing          | 0.25 vCPU / 0.5 GB, scales to zero after 15 min idle                |
| RDS sizing                 | `db.t4g.micro`, 20 GB gp3, Single-AZ                                |
| Region                     | `us-east-1`                                                         |
| Data transfer out          | 5 GB/month free, then standard tier                                 |

The assumptions are surfaced to the user with the estimate: *"At ~100k requests/mo and ~5 GB egress, you're looking at roughly $X/month."* Users who want different assumptions can ask, and we can extend `estimate_cost` to take overrides — but the v1 default is one canned tier so we ship.

**Return shape:**

```python
@dataclass
class CostLine:
    service: str       # "Lambda"
    monthly_usd: float
    note: str          # "100k invocations at 200ms/256MB"

@dataclass
class CostEstimate:
    lines: list[CostLine]
    total_monthly_usd: float
    assumptions: list[str]   # human-readable list, shown to the user
    is_fallback: bool        # True if AWS Pricing API failed and we used canned numbers
```

**Pricing API failure fallback:** if the Pricing API call fails (rate limit, region issue, network), fall back to a hard-coded table of "typical monthly cost per service at low traffic" and set `is_fallback=True`. The agent communicates the fallback to the user ("Couldn't reach the AWS pricing API — these are rough estimates").

### 5. Agent loop (`agent.py`)

Direct adaptation of `deploy-agent/agent.py:run_agent_loop`. Same `MAX_AGENT_ITERATIONS`, same `_serialize_content` trick, same SQLite-backed session persistence. The only differences from deploy-agent are the system prompt and the tools registered.

The system prompt encodes the four-stage workflow:

1. **Ingest:** ask the user for a GitHub URL if not provided. Call `clone_repo`.
2. **Validate:** call `analyze_repo`. Present the `summary` *verbatim*. Ask the user to confirm or correct.
3. **Recommend:** call `recommend_architecture` with the (possibly corrected) profile. Walk the user through each service with its purpose.
4. **Estimate:** call `estimate_cost`. Show the breakdown and total. Show the assumptions inline.

The system prompt explicitly forbids the agent from inventing services not present in the catalog or pricing not returned by `estimate_cost`. This is what stops the LLM from hallucinating "DynamoDB and ElastiCache for $42/month" out of nowhere.

### 6. Chat UI (`static/index.html`)

Single-file HTML+CSS+JS, same pattern as `deploy-agent/static/index.html`. No file dropzone (we don't take file uploads in this app — only a URL). One-column chat. Reuses the same DOM-based markdown renderer (no `innerHTML`, URL scheme validation), the same typing indicator, the same GovTech color palette. The hero copy is different: *"Drop a repo, get an AWS plan."*

The chat input accepts plain text; the agent extracts the URL from it. We do **not** add a separate "GitHub URL" field — letting the user paste a URL into chat keeps the surface uniform with the agent's conversational style and supports later flows ("here's the URL and also, ignore the docs folder").

## Data flow (the happy path)

```
User: "https://github.com/some/repo"
  ↓
agent → clone_repo(url)
  ↓ { path, repo_name, file_count, size_mb }
agent → analyze_repo(path)
  ↓ RepoProfile { app_type: "spa_with_api", summary: "..." }
agent → user: "Here's what I see: <summary>. Sound right?"
  ↓
User: "Yes" / "Actually it also uses Postgres"
  ↓ (if correction: agent updates profile.has_database_hints before next call)
agent → recommend_architecture(profile)
  ↓ Architecture { services: [S3, CloudFront, API GW, Lambda], notes: [...] }
agent → user: "I'd use S3 + CloudFront for the frontend and Lambda + API Gateway for the API because <reasoning>."
  ↓
agent → estimate_cost(architecture)
  ↓ CostEstimate { lines: [...], total: $X, assumptions: [...] }
agent → user: "At ~100k requests/mo: S3 ~$0.10, CloudFront ~$0.50, API Gateway ~$0.35, Lambda ~$0.10 → ~$1.05/month total. Assumptions: ..."
```

## Error handling

Same `{summary, details}` contract as `deploy-agent/tools.py`. Every tool returns either a success dict or `{summary: "<friendly one-liner>", details: "<raw error for if-they-ask>"}`. The system prompt instructs the agent to surface `summary` verbatim and offer `details` only if asked.

Specific failures the system prompt handles:

| Failure                              | Agent response                                                                 |
| ------------------------------------ | ------------------------------------------------------------------------------ |
| Invalid URL                          | Ask for a `https://github.com/<owner>/<repo>` URL                              |
| Private repo / 404                   | "I can only see public repos right now."                                       |
| Repo too large                       | "Point me at the subfolder for the app you want to deploy."                    |
| `unknown` app type                   | Ask the user to describe what the app does, then continue                      |
| Pricing API failure                  | Surface the fallback estimates with an "AWS pricing API was down" note         |
| Anything else (network, panic)       | Generic apology + retry suggestion, full error in `details`                    |

## Testing

Test count baseline parity with `deploy-agent` is the target. Concretely:

**Unit tests (no external calls):**

- `tests/test_analyzer.py` — fixture repos in `tests/fixtures/{static_site, node_api, python_api, dockerized_web, fullstack_with_db}/`. For each fixture, assert the `RepoProfile` matches expectations. This is the single highest-value test file in the project; the catalog is only as good as the classifier.
- `tests/test_patterns.py` — given each `RepoProfile` shape, assert `recommend_architecture` returns the right pattern and services. Pure function, fast.
- `tests/test_pricing.py` — mock `boto3.client("pricing")`, assert cost lines are computed correctly. Include the fallback path.
- `tests/test_tools.py` — `clone_repo` validation (URL parsing, size/file-count guards), error-message classification. Network calls mocked.

**Smoke test (real network):**

- `scripts/smoke-test.sh` — clone a known small public repo (the existing `examples/sample-site/` is the obvious candidate once the parent repo is published as a tagged ref, or any small public static-site repo as a stopgap), run the full chain, assert a non-zero cost estimate and an expected service list. Mirrors `deploy-agent/scripts/smoke-test.sh` in shape.

## Open items deferred to later phases

- Multi-architecture comparison ("show me both App Runner and Lambda")
- Traffic-tier overrides ("what if I get 10M requests/mo?")
- Private repo support (would need GitHub OAuth)
- IaC code generation (would naturally consume the `Architecture` object)
- Hand-off to a deploy flow (the obvious next phase; `Architecture` is designed to be consumable)
- Security and Well-Architected review (separate skills already exist)
- LLM-driven catalog extension (the agent suggesting new patterns it noticed)

## Open questions for the user

None for this phase. All design decisions above are made; corrections welcome at review time.
