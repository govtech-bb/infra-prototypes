"""Cost estimator.

v1: uses a curated fallback table only. The `is_fallback` field on
CostEstimate is always True. A Phase 1.5 plan will add live AWS Pricing
API lookups; this module is structured so that becomes additive.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from patterns import Architecture


@dataclass
class CostLine:
    service: str
    monthly_usd: float
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CostEstimate:
    lines: list[CostLine] = field(default_factory=list)
    total_monthly_usd: float = 0.0
    assumptions: list[str] = field(default_factory=list)
    is_fallback: bool = True

    def to_dict(self) -> dict:
        return {
            "lines": [line.to_dict() for line in self.lines],
            "total_monthly_usd": self.total_monthly_usd,
            "assumptions": self.assumptions,
            "is_fallback": self.is_fallback,
        }


# Service → (monthly_usd, note) at the prototype baseline below.
# Numbers are rough order-of-magnitude estimates and are deliberately
# rounded — they're a starting point, not a quote.
_FALLBACK_PRICES: dict[str, tuple[float, str]] = {
    "S3": (0.10, "~1 GB stored + ~10k GET requests"),
    "CloudFront": (0.50, "~5 GB data out + ~100k requests (free tier covers most prototypes)"),
    "API Gateway": (0.35, "~100k HTTP API requests"),
    "Lambda": (0.10, "~100k invocations at 256 MB / 200 ms"),
    # ECS Fargate runs 24/7 — no scale-to-zero. ~$0.04/vCPU-hr + ~$0.0044/GB-hr;
    # 0.25 vCPU / 0.5 GB / 730 hr/mo ≈ $9. Rounded to $9 to keep prototype-tier honest.
    "ECS Fargate": (9.00, "0.25 vCPU / 0.5 GB, runs 24/7 (Fargate doesn't pause idle tasks)"),
    "RDS PostgreSQL": (12.00, "db.t4g.micro, 20 GB gp3, Single-AZ"),
    "EventBridge Scheduler": (0.00, "<1k invocations/mo is in the free tier"),
}

# Per-service sizing/traffic assumptions. Only contribute to the user-facing
# `assumptions` list when the matching service is actually in the architecture.
# (The previous _BASELINE_ASSUMPTIONS list returned all of these unconditionally,
# which leaked CloudFront/Lambda assumptions into App-Runner-only estimates.)
_PER_SERVICE_ASSUMPTIONS: dict[str, str] = {
    "CloudFront": "~5 GB CloudFront egress per month",
    "Lambda": "Lambda: 256 MB memory, 200 ms avg duration",
    "ECS Fargate": "ECS Fargate: 0.25 vCPU / 0.5 GB, runs 24/7 (no scale-to-zero)",
    "RDS PostgreSQL": "RDS: db.t4g.micro, 20 GB gp3, Single-AZ",
}

_ALWAYS_ASSUMPTIONS_HEAD = ["Region: us-east-1"]
_ALWAYS_ASSUMPTIONS_TAIL = [
    "Numbers are rough starting points — actual cost depends on real traffic."
]


def _build_assumptions(architecture: Architecture) -> list[str]:
    seen: set[str] = set()
    per_service: list[str] = []
    for svc in architecture.services:
        line = _PER_SERVICE_ASSUMPTIONS.get(svc.aws_service)
        if line and line not in seen:
            per_service.append(line)
            seen.add(line)
    return _ALWAYS_ASSUMPTIONS_HEAD + per_service + _ALWAYS_ASSUMPTIONS_TAIL


def estimate(architecture: Architecture) -> CostEstimate:
    if not architecture.services:
        return CostEstimate(
            lines=[],
            total_monthly_usd=0.0,
            assumptions=[],
            is_fallback=True,
        )

    lines: list[CostLine] = []
    for svc in architecture.services:
        usd, note = _FALLBACK_PRICES.get(svc.aws_service, (0.0, "no price available"))
        lines.append(CostLine(service=svc.aws_service, monthly_usd=round(usd, 2), note=note))

    total = round(sum(line.monthly_usd for line in lines), 2)
    return CostEstimate(
        lines=lines,
        total_monthly_usd=total,
        assumptions=_build_assumptions(architecture),
        is_fallback=True,
    )
