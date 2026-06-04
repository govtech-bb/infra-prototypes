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


# ── Shared notes (audit follow-up: cross-cutting gaps surfaced in the SME pass) ───
# These get appended to multiple patterns to avoid copy-paste drift. Edit here,
# applies everywhere it's referenced.

_CUSTOM_DOMAIN_NOTE = (
    "Custom domain (optional): create a Route53 public hosted zone (~$0.50/mo) "
    "and an ACM certificate (free). For CloudFront and edge endpoints, request "
    "the cert in us-east-1; for ALB and regional endpoints, request it in the "
    "same region as the resource. Point your domain via a Route53 alias record."
)

_CLOUDWATCH_RETENTION_NOTE = (
    "CloudWatch Logs and basic metrics are on by default and stay in the free "
    "tier at prototype traffic. One gotcha: log groups default to never-expire "
    "retention — set 7- or 14-day retention to avoid surprise costs as the app "
    "accumulates noise."
)

_VPC_BASELINE_NOTE = (
    "VPC baseline: 1 VPC with 2 public subnets across 2 AZs (and 2 private "
    "subnets if you have a database). Fargate task in a public subnet pulls "
    "from public ECR via the Internet Gateway — no NAT Gateway needed for "
    "prototypes. NAT Gateway is the silent ~$32/mo budget killer; avoid until "
    "you actually need outbound internet from a private resource."
)


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
        notes=[
            "CloudFront reads from S3 via Origin Access Control (OAC); the "
            "bucket stays private. OAC is the current AWS recommendation — "
            "older Origin Access Identity (OAI) is in maintenance.",
            _CUSTOM_DOMAIN_NOTE,
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
                aws_service="API Gateway (HTTP API)",
                purpose=(
                    "Public HTTPS endpoint for your backend. HTTP API "
                    "specifically — it's ~70% cheaper than REST API and the "
                    "right default for almost all prototype workloads."
                ),
                sizing={"requests_per_month": 100_000},
            ),
            ArchitectureService(
                aws_service="Lambda",
                purpose="Runs your backend on demand; scales to zero.",
                sizing={"requests_per_month": 100_000, "memory_mb": 256, "duration_ms": 200},
            ),
        ],
        notes=[
            "CloudFront reads from S3 via Origin Access Control (OAC); the bucket stays private.",
            "For client-side routing (React Router, Vue Router, etc.), "
            "configure CloudFront to rewrite 403 / 404 responses to "
            "/index.html with HTTP 200 — otherwise refreshing a deep link "
            "returns an error.",
            "CloudFront proxies /api/* to API Gateway as a separate cache "
            "behavior so the SPA and the API live under one origin (no CORS "
            "to manage).",
            _CUSTOM_DOMAIN_NOTE,
            _CLOUDWATCH_RETENTION_NOTE,
        ],
    ),
    "node_api": Architecture(
        pattern="node_api",
        services=[
            ArchitectureService(
                aws_service="API Gateway (HTTP API)",
                purpose=(
                    "Public HTTPS endpoint that routes to your Lambda functions. "
                    "HTTP API specifically — ~70% cheaper than REST API and the "
                    "right default for almost all prototype workloads."
                ),
                sizing={"requests_per_month": 100_000},
            ),
            ArchitectureService(
                aws_service="Lambda",
                purpose="Runs your Node.js API on demand; truly scales to zero.",
                sizing={"requests_per_month": 100_000, "memory_mb": 256, "duration_ms": 200},
            ),
        ],
        notes=[
            "Alternative: ECS Fargate if you have long-lived connections, "
            "WebSockets, or you need to run a process that doesn't fit Lambda's "
            "15-minute / 10 GB envelope.",
            _CUSTOM_DOMAIN_NOTE,
            _CLOUDWATCH_RETENTION_NOTE,
        ],
    ),
    "python_api": Architecture(
        pattern="python_api",
        services=[
            ArchitectureService(
                aws_service="API Gateway (HTTP API)",
                purpose=(
                    "Public HTTPS endpoint that routes to your Lambda functions. "
                    "HTTP API specifically — ~70% cheaper than REST API and the "
                    "right default for almost all prototype workloads."
                ),
                sizing={"requests_per_month": 100_000},
            ),
            ArchitectureService(
                aws_service="Lambda",
                purpose="Runs your Python API on demand; scales to zero.",
                sizing={"requests_per_month": 100_000, "memory_mb": 256, "duration_ms": 200},
            ),
        ],
        notes=[
            "Recommended adapter: AWS Lambda Web Adapter (LWA) — zero-code-"
            "change wrapper for FastAPI / Flask / Django. Same container runs "
            "locally and on Lambda, so the code can move to Fargate later "
            "without changes. Ship as a Lambda layer or container extension.",
            "Mangum is the pure-Python alternative if you'd rather not add a "
            "layer; both work, LWA is the current AWS-blessed path.",
            "Alternative: ECS Fargate if your framework has slow cold starts "
            "or long-lived background work.",
            _CUSTOM_DOMAIN_NOTE,
            _CLOUDWATCH_RETENTION_NOTE,
        ],
    ),
    "dockerized_web": Architecture(
        pattern="dockerized_web",
        services=[
            ArchitectureService(
                aws_service="Application Load Balancer",
                purpose=(
                    "Front door for the Fargate task. Terminates TLS via an "
                    "ACM cert, routes to the task on a target group, runs "
                    "health checks, and gives you a stable hostname (Fargate "
                    "ENIs rotate on every deploy)."
                ),
                sizing={"lcu_per_month": "<1 at prototype traffic"},
            ),
            ArchitectureService(
                aws_service="ECS Fargate",
                purpose=(
                    "Runs your container as a managed service — no cluster "
                    "management, no EC2 hosts to patch."
                ),
                sizing={"vcpu": 0.25, "memory_gb": 0.5, "requests_per_month": 100_000},
            ),
        ],
        notes=[
            "Alternative: ECS Express Mode for App-Runner-style simpler config "
            "— AWS now guides new container workloads to Express Mode or full "
            "Fargate rather than App Runner.",
            _VPC_BASELINE_NOTE,
            _CUSTOM_DOMAIN_NOTE,
            _CLOUDWATCH_RETENTION_NOTE,
        ],
    ),
    "fullstack_with_db": Architecture(
        pattern="fullstack_with_db",
        services=[
            ArchitectureService(
                aws_service="Application Load Balancer",
                purpose=(
                    "Front door for the Fargate task. Terminates TLS via an "
                    "ACM cert, routes to the task on a target group, runs "
                    "health checks, and gives you a stable hostname / IP "
                    "(Fargate tasks rotate ENIs on every deploy)."
                ),
                sizing={"lcu_per_month": "<1 at prototype traffic"},
            ),
            ArchitectureService(
                aws_service="ECS Fargate",
                purpose="Hosts your web app container — managed, no EC2 hosts to patch.",
                sizing={"vcpu": 0.25, "memory_gb": 0.5, "requests_per_month": 100_000},
            ),
            ArchitectureService(
                aws_service="RDS PostgreSQL",
                purpose="Managed database (db.t4g.micro, 20 GB gp3, Single-AZ).",
                sizing={"instance_class": "db.t4g.micro", "storage_gb": 20},
            ),
        ],
        notes=[
            "Alternative: Aurora Serverless v2 (min 0.5 ACU) for the DB if you "
            "want it to auto-pause during idle periods.",
            "Alternative: ECS Express Mode for simpler config than full "
            "Fargate — AWS now guides new container workloads to Express Mode "
            "or full Fargate rather than App Runner.",
            _VPC_BASELINE_NOTE,
            "Put RDS in a private subnet with a DB security group that only "
            "accepts traffic from the Fargate task SG — never expose the DB "
            "to 0.0.0.0/0 even for dev convenience.",
            "Secrets: default DATABASE_URL and other credentials to SSM "
            "Parameter Store (Standard tier, free) and reference them from "
            "your ECS task definition's `secrets:` block. Switch to AWS "
            "Secrets Manager (~$0.40/secret/mo) only if you need automatic "
            "rotation (built-in for RDS), cross-account sharing, or KMS-per-"
            "secret separation.",
            _CUSTOM_DOMAIN_NOTE,
            _CLOUDWATCH_RETENTION_NOTE,
        ],
    ),
    "nextjs_amplify_hosting": Architecture(
        pattern="nextjs_amplify_hosting",
        services=[
            ArchitectureService(
                aws_service="AWS Amplify (Gen 2)",
                purpose=(
                    "AWS Amplify Gen 2 — the current-generation managed host for "
                    "Next.js / Nuxt / Remix. TypeScript-first, code-defined "
                    "backend (CDK under the hood), branch-based deploys with "
                    "preview environments, and SSR / API routes / middleware "
                    "handled out of the box. Scales to near-zero between requests."
                ),
                sizing={
                    "requests_per_month": 100_000,
                    "data_out_gb": 5,
                    "ssr_memory_mb": 512,
                },
            ),
        ],
        notes=[
            "We're recommending Amplify Gen 2 specifically — Gen 1 (the older "
            "console-driven Amplify Hosting) is in maintenance mode.",
            "Amplify Gen 2 also bundles a data layer (AppSync resolvers "
            "backed by DynamoDB by default, OR by an existing RDS / Aurora "
            "SQL cluster via the SQL data source), auth (Cognito), file "
            "storage (S3), and Lambda functions — all defined in TypeScript "
            "via `defineBackend({ auth, data, storage, ... })` in "
            "`amplify/backend.ts`. The hosting cost above is hosting only; "
            "bundled primitives meter separately but usually land in the AWS "
            "free tier at prototype traffic.",
            "If your repo already uses an external backend (Supabase, "
            "Firebase, PlanetScale, Neon), you can keep it — Amplify Hosting "
            "just runs your app, the bundled data layer is opt-in. To move "
            "off your external DB later, you can adopt AppSync+DynamoDB "
            "incrementally or migrate to ECS Fargate + RDS PostgreSQL.",
            "First-time setup: connect GitHub in the Amplify console (one-"
            "time OAuth per AWS account). After that, push to a tracked "
            "branch deploys automatically; pull requests get isolated "
            "preview environments with their own URLs and backend resources "
            "— this is one of Gen 2's biggest day-one wins.",
            "Per-branch env vars and secrets are set in the Amplify console "
            "under branch settings. For runtime secrets, prefer "
            "`defineFunction` with parameter access or SSM Parameter Store "
            "— never commit secrets to `backend.ts`.",
            "Custom domains are managed inside the Amplify console — no "
            "separate Route53 work needed if the zone already exists in "
            "your account. Amplify provisions the ACM cert and DNS records "
            "automatically.",
            "Alternative: ECS Fargate + RDS PostgreSQL if you want a "
            "traditional Postgres database (vs DynamoDB) and BYO containers "
            "instead of managed hosting.",
        ],
    ),
    "tiny_container": Architecture(
        pattern="tiny_container",
        services=[
            ArchitectureService(
                aws_service="Lambda",
                purpose=(
                    "Runs your Docker image as a Lambda function (up to 10 GB "
                    "image). AWS Lambda Web Adapter (LWA) wraps the container's "
                    "HTTP server so Lambda invokes it via the standard event "
                    "model — same image runs locally and on Lambda, no code "
                    "changes. Scales to zero between requests."
                ),
                sizing={"memory_mb": 256, "duration_ms": 200, "requests_per_month": 100_000},
            ),
            ArchitectureService(
                aws_service="Lambda Function URL",
                purpose=(
                    "Public HTTPS endpoint built into Lambda — no API Gateway "
                    "needed. Free. Auth options: IAM-signed or none."
                ),
                sizing={"requests_per_month": 100_000},
            ),
        ],
        notes=[
            "This is the cheap, scale-to-zero answer when you have a Dockerfile "
            "but don't need always-on Fargate. Best for: prototypes, internal "
            "tools, low-traffic public APIs, webhook receivers.",
            "Constraints to know: request timeout caps at 15 min, memory caps "
            "at 10 GB, image caps at 10 GB. Cold starts run 1-3s for container "
            "images vs <500ms for zip-based Lambdas — fine for human-facing "
            "requests, less fine for sub-100ms latency SLOs.",
            "Lambda Web Adapter is the AWS-blessed wrapper. Supports Express, "
            "FastAPI, Flask, Django, Gin, Spring Boot, Next.js standalone, "
            "anything that listens on a port. Ship as a Lambda layer or as a "
            "base-image extension.",
            "Alternative: ECS Fargate + ALB (~$25/mo) if you need long-lived "
            "connections (WebSockets, SSE), sustained high traffic where Lambda "
            "compute would outprice Fargate, or sub-second cold starts. Ask me "
            "for the 'Fargate alternative' and I'll switch.",
            "For a custom domain, front the Function URL with CloudFront and "
            "an ACM cert in us-east-1 (Function URLs don't take custom domains "
            "directly).",
            _CLOUDWATCH_RETENTION_NOTE,
        ],
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
        notes=[
            "Alternative: ECS Fargate scheduled task if jobs run longer than 15 min.",
            "Multi-step alternative: if your job has multiple stages with branching "
            "or retry logic (or your repo uses Prefect / Dagster / Airflow DAGs), "
            'use `pattern_override="workflow_worker"` instead — that adds Step '
            "Functions Express to orchestrate between Lambda task workers.",
            "Queue-driven alternative: if a web app or external service pushes "
            "async jobs (rather than a schedule driving them), use "
            '`pattern_override="queue_worker"` — that pattern uses SQS as the '
            "trigger instead of EventBridge.",
            _CLOUDWATCH_RETENTION_NOTE,
        ],
    ),
    "workflow_worker": Architecture(
        pattern="workflow_worker",
        services=[
            ArchitectureService(
                aws_service="Step Functions (Express)",
                purpose=(
                    "Orchestrates your multi-step workflow. Express Workflows are "
                    "the right default for high-throughput, short-duration jobs "
                    "(≤ 5 min execution): ~100x cheaper than Standard Workflows "
                    "and priced per state transition + execution duration rather "
                    "than per execution count. Standard Workflows are the right "
                    "choice for human-in-the-loop / approval flows or when you "
                    "need exactly-once semantics and audit history beyond 5 min."
                ),
                sizing={"executions_per_month": 720, "transitions_per_execution": 5},
            ),
            ArchitectureService(
                aws_service="Lambda",
                purpose=(
                    "Task workers invoked by Step Functions. Each step in your "
                    "workflow maps to a Lambda task state; Step Functions handles "
                    "the retry / catch / parallel branching in ASL (Amazon States "
                    "Language) — your Lambda code stays simple."
                ),
                sizing={"memory_mb": 512, "duration_ms": 5000, "invocations_per_month": 720},
            ),
        ],
        notes=[
            "Express vs Standard: Express (this pattern) caps at 5 min total "
            "execution time, uses at-least-once delivery, and costs ~$0.10/mo "
            "at 720 executions x 5 transitions. Standard Workflows support "
            "executions up to 1 year, exactly-once task execution, and a "
            "built-in visual audit trail — choose Standard when you need "
            "human approval steps or long-running coordination.",
            "When to use workflow_worker vs worker vs queue_worker: "
            "schedule-driven single-script job → `worker`; "
            "multi-step job with branching / retry / parallel branches (or "
            "repo uses Prefect / Dagster / Airflow) → `workflow_worker`; "
            "web app pushes async jobs via a queue → `queue_worker`.",
            "ASL (Amazon States Language) is JSON/YAML — Step Functions Visual "
            "Workflow Studio in the AWS console lets you draw the state machine "
            "and export valid ASL without writing it by hand.",
            _CLOUDWATCH_RETENTION_NOTE,
        ],
    ),
}


_SSR_DEFAULT_FRONTEND_FRAMEWORKS = {"next", "nuxt", "remix"}


def recommend(profile: RepoProfile) -> Architecture:
    """Map a RepoProfile to an Architecture.

    Routing rules (in order):
    1. `unknown` → empty architecture with a "tell me about the app" note.
    2. `spa_with_api` + `has_database_hints` → upgrade to `fullstack_with_db`.
    3. SSR-default frontend (Next.js / Nuxt / Remix) with no Dockerfile →
       route to `nextjs_amplify_hosting`. Amplify Hosting handles SSR + API
       routes natively, scales to near-zero, and lets the user keep their
       managed backend (Supabase / Firebase / etc.) instead of forcing a DB
       migration. A Dockerfile is the escape hatch: if the user committed
       one they likely want containers (Fargate), not a managed host.
    4. Otherwise → dict lookup against `_CATALOG`.
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

    if (
        pattern_key in ("spa_with_api", "fullstack_with_db")
        and (set(profile.frameworks) & _SSR_DEFAULT_FRONTEND_FRAMEWORKS)
        and not profile.has_dockerfile
    ):
        pattern_key = "nextjs_amplify_hosting"

    # tiny_container is the default for dockerized_web at prototype scale:
    # Lambda + LWA + Function URL is ~12x cheaper than ALB + Fargate
    # ($0.10 vs $25/mo) and scales to zero. Users who genuinely need
    # always-on Fargate (long-lived connections, sustained traffic, sub-
    # second cold starts) can request the original ALB+Fargate path via
    # `pattern_override="dockerized_web"`.
    if pattern_key == "dockerized_web":
        pattern_key = "tiny_container"

    return _CATALOG[pattern_key]
