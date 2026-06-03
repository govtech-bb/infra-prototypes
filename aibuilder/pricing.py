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
    # HTTP API: $1/M requests in us-east-1. 100k req = $0.10.
    # (Previous $0.35 was REST-API priced and silently mislabeled.)
    "API Gateway (HTTP API)": (0.10, "~100k HTTP API requests at $1/M"),
    # Lambda: 100k inv * $0.20/M = $0.02 request cost, plus 100k * 0.2s * 0.25 GB =
    # 5,000 GB-s * $0.0000166667 = $0.083 compute. Total ~= $0.10. Free tier covers
    # most of this; $0.10 is the honest fully-charged number.
    "Lambda": (0.10, "~100k invocations at 256 MB / 200 ms (compute dominates over requests)"),
    # ECS Fargate runs 24/7 — no scale-to-zero. ~$0.04/vCPU-hr + ~$0.0044/GB-hr;
    # 0.25 vCPU / 0.5 GB / 730 hr/mo ≈ $9. Rounded to $9 to keep prototype-tier honest.
    "ECS Fargate": (9.00, "0.25 vCPU / 0.5 GB, runs 24/7 (Fargate doesn't pause idle tasks)"),
    # ALB: fixed hourly charge $0.0225 * 730 = $16.42 + LCU. LCU at prototype
    # traffic (<25 new conns/sec, <3000 active conns, <1 GB/hr, <1000 rules)
    # is well under 1 LCU * $0.008/hr * 730 = $5.84, so call it ~$16/mo round.
    "Application Load Balancer": (
        16.00,
        "fixed hourly charge ~$16/mo (LCU usage is negligible at prototype traffic)",
    ),
    # Amplify Gen 2 hosting: build minutes (1000/mo free) + hosting served
    # ($0.15/GB, first 15 GB free) + SSR ($0.30/M requests + $0.20/GB-hr).
    # For 100k req + 5 GB served + ~1 GB-hr SSR: request cost $0.03, SSR
    # compute $0.20, hosting + build usually inside free tier → ~$0.25 actual.
    # $1 is a conservative round that covers a bit more SSR traffic and the
    # occasional build-minute overflow on busy branch-preview workflows.
    "AWS Amplify (Gen 2)": (
        1.00,
        "~100k requests + ~5 GB served + ~1 GB-hr SSR compute (most of this lands in free tier)",
    ),
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
    "Application Load Balancer": (
        "ALB: ~$16/mo fixed (LCU usage at prototype traffic is in the noise)"
    ),
    "AWS Amplify (Gen 2)": (
        "AWS Amplify (Gen 2): ~100k requests/mo, ~5 GB served, SSR via managed "
        "compute (Amplify free tier covers most prototype traffic)"
    ),
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
