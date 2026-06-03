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
                aws_service="API Gateway",
                purpose="Public HTTPS endpoint that routes to your Lambda functions.",
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
                aws_service="API Gateway",
                purpose="Public HTTPS endpoint that routes to your Lambda functions.",
                sizing={"requests_per_month": 100_000},
            ),
            ArchitectureService(
                aws_service="Lambda",
                purpose=(
                    "Runs your Python API via Mangum (FastAPI / Flask adapter); scales to zero."
                ),
                sizing={"requests_per_month": 100_000, "memory_mb": 256, "duration_ms": 200},
            ),
        ],
        notes=[
            "Alternative: ECS Fargate if your framework has slow cold starts or "
            "long-lived background work.",
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
            "Alternative: ECS Express Mode if you want App-Runner-style simpler "
            "config (App Runner itself is being deprecated by AWS).",
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
            "Alternative: ECS Express Mode for simpler config than full Fargate "
            "(App Runner is being deprecated by AWS).",
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
