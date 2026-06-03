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
        notes=[
            "CloudFront reads from S3 via Origin Access Control (OAC); the "
            "bucket stays private. OAC is the current AWS recommendation — "
            "older Origin Access Identity (OAI) is in maintenance.",
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
        ],
    ),
    "dockerized_web": Architecture(
        pattern="dockerized_web",
        services=[
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
        ],
    ),
    "fullstack_with_db": Architecture(
        pattern="fullstack_with_db",
        services=[
            ArchitectureService(
                aws_service="ECS Fargate",
                purpose="Hosts your web app container behind an Application Load Balancer.",
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
            "Amplify Gen 2 also bundles a data layer (AppSync + DynamoDB), "
            "auth (Cognito), file storage (S3), and Lambda functions — all "
            "defined in TypeScript alongside your app via `defineData`, "
            "`defineAuth`, etc. The $3/mo above is hosting only; the bundled "
            "primitives meter separately but are usually covered by the AWS "
            "free tier at prototype traffic.",
            "If your repo already uses an external backend (Supabase, "
            "Firebase, PlanetScale, Neon), you can keep it — Amplify Hosting "
            "just runs your app, the bundled data layer is opt-in. To move "
            "off your external DB later, you can adopt AppSync+DynamoDB "
            "incrementally or migrate to ECS Fargate + RDS PostgreSQL.",
            "Alternative: ECS Fargate + RDS PostgreSQL if you want a "
            "traditional Postgres database (vs DynamoDB) and BYO containers "
            "instead of managed hosting.",
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
        notes=["Alternative: ECS Fargate scheduled task if jobs run longer than 15 min."],
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

    return _CATALOG[pattern_key]
