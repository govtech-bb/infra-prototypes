"""Cost estimator.

v1: uses a curated fallback table only.
v1.5 (Phase 1.5): adds live AWS Pricing API lookups for Lambda. The
`is_fallback` field on CostEstimate is now meaningful:
  - False: at least one service price came from the live Pricing API.
  - True: every price came from the static fallback table (or the service
    was unrecognised and returned $0).

Currently only Lambda is live-priced. All other services use the static
fallback table. To add live pricing for service X, write a
`_live_price_x()` function that returns ``(monthly_usd, note)`` on
success or ``None`` on any error, then register it in ``_LIVE_RESOLVERS``.
No other code changes are needed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from patterns import Architecture

logger = logging.getLogger(__name__)


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


# Service -> (monthly_usd, note) at the prototype baseline below.
# Numbers are rough order-of-magnitude estimates and are deliberately
# rounded -- they're a starting point, not a quote.
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
    # Lambda Function URLs are free -- no per-URL or per-request fee. Only the
    # underlying Lambda invocations meter (and those are priced under "Lambda").
    "Lambda Function URL": (
        0.00,
        "Function URLs are free -- only Lambda compute meters separately",
    ),
    # ECS Fargate runs 24/7 -- no scale-to-zero. ~$0.04/vCPU-hr + ~$0.0044/GB-hr;
    # 0.25 vCPU / 0.5 GB / 730 hr/mo ~= $9. Rounded to $9 to keep prototype-tier honest.
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
    # compute $0.20, hosting + build usually inside free tier -> ~$0.25 actual.
    # $1 is a conservative round that covers a bit more SSR traffic and the
    # occasional build-minute overflow on busy branch-preview workflows.
    "AWS Amplify (Gen 2)": (
        1.00,
        "~100k requests + ~5 GB served + ~1 GB-hr SSR compute (most of this lands in free tier)",
    ),
    "RDS PostgreSQL": (12.00, "db.t4g.micro, 20 GB gp3, Single-AZ"),
    "EventBridge Scheduler": (0.00, "<1k invocations/mo is in the free tier"),
    # Step Functions Express: $1/M state transitions + $0.00001667/GB-s execution.
    # 720 exec/mo x 5 transitions = 3,600 transitions -- essentially free.
    # $0.10/mo is a conservative round to cover small compute overhead.
    "Step Functions (Express)": (
        0.10,
        "~720 executions x 5 transitions/exec = 3,600 transitions ($1/M rate) -- "
        "effectively free at prototype scale; $0.10 is a conservative round",
    ),
    # SQS Standard: first 1M requests/mo free. At prototype scale (100k messages)
    # this is $0. FIFO queues are slightly more expensive but not modeled here.
    "SQS": (0.00, "first 1M requests/mo are free; at prototype scale (100k messages) this is $0"),
    # Internal ALB: same fixed hourly charge as a public ALB ($0.0225/hr * 730 = ~$16/mo).
    # The internal vs public distinction is scheme-only; pricing is identical.
    "Application Load Balancer (internal)": (
        16.00,
        "same pricing as a public ALB: fixed hourly ~$16/mo (LCU at prototype traffic is negligible)",
    ),
    # Cognito User Pool: free for first 50,000 MAU. Internal tools comfortably stay in free tier.
    "Cognito User Pool": (0.00, "free for first 50,000 MAU; internal tools stay well inside this"),
    # AWS WAF: $5/mo WebACL + managed rule group charges + per-request meter.
    # At prototype traffic the per-request meter is negligible; ~$10/mo is the conservative round.
    "AWS WAF": (
        10.00,
        "~$5/mo WebACL + AWS Managed Rules; per-request meter negligible at prototype traffic",
    ),
    # Secrets Manager: $0.40/secret/mo. One secret (DATABASE_URL) = $0.40/mo.
    "Secrets Manager": (0.40, "~$0.40/secret/mo; 1 secret (DATABASE_URL with automatic rotation)"),
    # Route53 Private Hosted Zone: $0.50/zone/mo.
    "Route53 Private Hosted Zone": (0.50, "~$0.50/mo per private hosted zone"),
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
    "Numbers are rough starting points -- actual cost depends on real traffic."
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


# ── Live AWS Pricing API ──────────────────────────────────────────────────────

# Cache maps service name -> (monthly_usd, note) or None.
# None means "we tried and failed -- don't retry this process lifetime."
_LIVE_PRICE_CACHE: dict[str, tuple[float, str] | None] = {}


def _live_price_lambda() -> tuple[float, str] | None:
    """Query the AWS Pricing API for Lambda request + compute rates.

    Returns (monthly_usd, note) for our prototype baseline (100k invocations,
    256 MB, 200 ms) or None on any error. Result is cached for the lifetime of
    the process; we don't retry on failure.

    The Pricing API is only available in us-east-1 regardless of the region
    where the Lambda actually runs. We look for two SKUs:
      - AWS-Lambda-Requests: per-request charge
      - AWS-Lambda-Duration: GB-second compute charge

    Prototype math (fully-charged, no free tier):
      requests = 100,000 x request_price_per_request
      compute  = 100,000 x 0.2 s x 0.25 GB x compute_price_per_gb_second
      total    = requests + compute
    """
    try:
        client = boto3.client("pricing", region_name="us-east-1")
        response = client.get_products(
            ServiceCode="AWSLambda",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": "us-east-1"},
                {"Type": "TERM_MATCH", "Field": "group", "Value": "AWS-Lambda-Requests"},
            ],
            MaxResults=10,
        )
        request_price = _extract_price_per_unit(response)

        response2 = client.get_products(
            ServiceCode="AWSLambda",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": "us-east-1"},
                {"Type": "TERM_MATCH", "Field": "group", "Value": "AWS-Lambda-Duration"},
            ],
            MaxResults=10,
        )
        compute_price = _extract_price_per_unit(response2)

        if request_price is None or compute_price is None:
            logger.warning("pricing: Lambda live lookup returned None for one or both SKUs")
            return None

        # 100k invocations * request_price (price is per request, not per million)
        request_cost = 100_000 * request_price
        # 100k inv * 0.2 s * 0.25 GB = 5,000 GB-s * compute_price_per_gb_s
        compute_cost = 100_000 * 0.2 * 0.25 * compute_price
        total = request_cost + compute_cost

        today = datetime.now(tz=UTC).date()
        note = f"live: ~100k invocations at 256 MB / 200 ms (queried {today})"
        return (round(total, 2), note)

    except (BotoCoreError, ClientError, Exception):
        logger.warning("pricing: Lambda live lookup failed", exc_info=True)
        return None


def _extract_price_per_unit(response: dict) -> float | None:
    """Walk a Pricing API get_products response and return the first USD
    pricePerUnit found, or None if the structure doesn't match expectations."""
    price_list = response.get("PriceList", [])
    if not price_list:
        return None
    for item_str in price_list:
        try:
            item = json.loads(item_str) if isinstance(item_str, str) else item_str
            on_demand = item.get("terms", {}).get("OnDemand", {})
            for _sku, term in on_demand.items():
                dims = term.get("priceDimensions", {})
                for _dim, dim_data in dims.items():
                    usd_str = dim_data.get("pricePerUnit", {}).get("USD")
                    if usd_str is not None:
                        price = float(usd_str)
                        if price > 0:
                            return price
        except (KeyError, ValueError, TypeError):
            continue
    return None


# Registry of live resolvers. Keys must match the service strings used in
# _FALLBACK_PRICES and Architecture.services[].aws_service.
# To add live pricing for a new service, write `_live_price_<name>()` and
# add an entry here. No other code changes are needed.
_LIVE_RESOLVERS: dict[str, Callable[[], tuple[float, str] | None]] = {
    "Lambda": _live_price_lambda,
}


def _resolve_price(service: str) -> tuple[float, str, bool]:
    """Return (monthly_usd, note, is_live).

    is_live=True means the number came from the live AWS Pricing API.
    is_live=False means we used the static fallback table (or returned the
    unknown-service default of $0).
    """
    resolver = _LIVE_RESOLVERS.get(service)
    if resolver is not None:
        if service not in _LIVE_PRICE_CACHE:
            _LIVE_PRICE_CACHE[service] = resolver()
        live = _LIVE_PRICE_CACHE[service]
        if live is not None:
            return (*live, True)

    fallback = _FALLBACK_PRICES.get(service)
    if fallback:
        return (*fallback, False)
    return (0.0, "no price available", False)


# ── Public API ────────────────────────────────────────────────────────────────


def estimate(architecture: Architecture) -> CostEstimate:
    if not architecture.services:
        return CostEstimate(
            lines=[],
            total_monthly_usd=0.0,
            assumptions=[],
            is_fallback=True,
        )

    lines: list[CostLine] = []
    any_live = False
    for svc in architecture.services:
        usd, note, is_live = _resolve_price(svc.aws_service)
        any_live = any_live or is_live
        lines.append(CostLine(service=svc.aws_service, monthly_usd=round(usd, 2), note=note))

    total = round(sum(line.monthly_usd for line in lines), 2)
    return CostEstimate(
        lines=lines,
        total_monthly_usd=total,
        assumptions=_build_assumptions(architecture),
        is_fallback=not any_live,
    )
