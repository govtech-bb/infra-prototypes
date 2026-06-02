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
    "App Runner": (5.00, "0.25 vCPU / 0.5 GB, scales to zero when idle"),
    "RDS PostgreSQL": (12.00, "db.t4g.micro, 20 GB gp3, Single-AZ"),
    "EventBridge Scheduler": (0.00, "<1k invocations/mo is in the free tier"),
}

_BASELINE_ASSUMPTIONS = [
    "Region: us-east-1",
    "~100,000 requests per month",
    "~5 GB CloudFront egress",
    "Lambda: 256 MB memory, 200 ms avg duration",
    "App Runner: 0.25 vCPU / 0.5 GB, scales to zero after idle",
    "RDS: db.t4g.micro, 20 GB gp3, Single-AZ",
    "Numbers are rough starting points — actual cost depends on real traffic.",
]


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
        assumptions=_BASELINE_ASSUMPTIONS,
        is_fallback=True,
    )
