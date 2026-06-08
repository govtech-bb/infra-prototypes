"""Tests for cost estimation."""

import json
from unittest.mock import MagicMock, patch

from analyzer import RepoProfile
from patterns import Architecture, ArchitectureService, recommend
from pricing import CostEstimate, CostLine, estimate


def test_static_site_estimate_is_reasonable():
    arch = recommend(RepoProfile(app_type="static_site"))
    result = estimate(arch)
    assert isinstance(result, CostEstimate)
    assert len(result.lines) == 2
    services = [line.service for line in result.lines]
    assert services == ["S3", "CloudFront"]
    # Static site at low traffic should be cheap — under $5/mo
    assert result.total_monthly_usd < 5.0
    assert result.is_fallback is True  # v1: always fallback
    assert any("us-east-1" in a for a in result.assumptions)


def test_fullstack_estimate_includes_rds_line():
    arch = recommend(RepoProfile(app_type="fullstack_with_db"))
    result = estimate(arch)
    services = [line.service for line in result.lines]
    assert "RDS PostgreSQL" in services
    rds_line = next(line for line in result.lines if line.service == "RDS PostgreSQL")
    assert rds_line.monthly_usd > 0  # RDS is not free


def test_fullstack_estimate_includes_alb_and_total_reflects_it():
    """Audit follow-up #12: ALB is now an explicit cost line (~$16/mo). The
    old total was ~$21 with ALB silently free; honest total is ~$37."""
    arch = recommend(RepoProfile(app_type="fullstack_with_db"))
    result = estimate(arch)
    services = [line.service for line in result.lines]
    assert "Application Load Balancer" in services
    alb_line = next(line for line in result.lines if line.service == "Application Load Balancer")
    assert alb_line.monthly_usd >= 15  # ALB minimum is the fixed hourly charge
    # Total should now reflect ALB + Fargate + RDS, not just Fargate + RDS.
    assert result.total_monthly_usd >= 35
    # Assumptions should mention ALB too.
    text = " | ".join(result.assumptions)
    assert "ALB" in text or "Load Balancer" in text


def test_unknown_pattern_returns_empty_estimate():
    arch = recommend(RepoProfile(app_type="unknown"))
    result = estimate(arch)
    assert result.lines == []
    assert result.total_monthly_usd == 0.0


def test_unrecognized_service_falls_through_with_zero():
    """If patterns.py is extended with a service we haven't priced yet,
    estimate should not crash — it should record a zero with a note."""
    arch = Architecture(
        pattern="custom",
        services=[ArchitectureService("FakeService", "demo", {})],
    )
    result = estimate(arch)
    assert len(result.lines) == 1
    assert result.lines[0].monthly_usd == 0.0
    assert "no price" in result.lines[0].note.lower()


def test_cost_line_dataclass():
    line = CostLine(service="S3", monthly_usd=0.10, note="1 GB stored")
    assert line.service == "S3"
    assert line.monthly_usd == 0.10
    assert line.note == "1 GB stored"


def test_api_gateway_priced_as_http_api():
    """Audit follow-up: catalog used to label the price as 'API Gateway'
    but the dollar amount was actually REST-API priced (~3.5x too high).
    Now the key is 'API Gateway (HTTP API)' at $0.10 / 100k requests."""
    arch = recommend(RepoProfile(app_type="node_api"))
    result = estimate(arch)
    services = [line.service for line in result.lines]
    assert "API Gateway (HTTP API)" in services
    apigw_line = next(line for line in result.lines if line.service == "API Gateway (HTTP API)")
    assert apigw_line.monthly_usd == 0.10


def test_amplify_priced_for_free_tier_mostly_covered():
    """Audit follow-up: Amplify Gen 2 was $3/mo; real usage at the catalog's
    sizing (100k req + 5 GB + 1 GB-hr SSR) is closer to $0.25 actual,
    bumped to $1 conservative round."""
    profile = RepoProfile(
        app_type="spa_with_api",
        frameworks=["next", "react"],
        has_dockerfile=False,
    )
    arch = recommend(profile)
    result = estimate(arch)
    amplify_line = next(line for line in result.lines if line.service == "AWS Amplify (Gen 2)")
    assert amplify_line.monthly_usd == 1.00


def test_assumptions_match_chosen_services():
    """Regression: prior to this fix, estimate() always returned the full
    `_BASELINE_ASSUMPTIONS` list — so a fullstack_with_db estimate (App
    Runner + RDS, no CloudFront/Lambda) still leaked CloudFront and Lambda
    assumptions into the user-facing output. Surfaced during real testing
    against the st-thomas-sign-in repo.
    """
    fullstack = estimate(recommend(RepoProfile(app_type="fullstack_with_db")))
    text = " | ".join(fullstack.assumptions)
    # Region + disclaimer always present.
    assert "us-east-1" in text
    assert "rough" in text.lower()
    # Services that ARE in the architecture get their assumptions.
    assert "Fargate" in text
    assert "RDS" in text
    # Services that are NOT in the architecture must NOT contribute assumptions.
    assert "CloudFront" not in text, (
        "fullstack_with_db has no CloudFront — its assumption shouldn't leak through"
    )
    assert "Lambda" not in text, (
        "fullstack_with_db has no Lambda — its assumption shouldn't leak through"
    )
    # App Runner is deprecated and removed from every pattern.
    assert "App Runner" not in text

    static = estimate(recommend(RepoProfile(app_type="static_site")))
    static_text = " | ".join(static.assumptions)
    assert "CloudFront" in static_text  # is in static_site arch
    assert "Fargate" not in static_text
    assert "RDS" not in static_text
    assert "Lambda" not in static_text

    spa = estimate(recommend(RepoProfile(app_type="spa_with_api")))
    spa_text = " | ".join(spa.assumptions)
    assert "CloudFront" in spa_text
    assert "Lambda" in spa_text
    assert "Fargate" not in spa_text
    assert "RDS" not in spa_text


# ── Item 15: workflow_worker pricing ─────────────────────────────────────────


def test_workflow_worker_step_functions_is_cheap():
    """Item #15: Step Functions Express at prototype scale (720 executions x
    5 transitions) is essentially free -- priced at $0.10/mo as a conservative
    round. Total for workflow_worker should be under $1/mo."""
    from patterns import _CATALOG
    from pricing import estimate

    arch = _CATALOG["workflow_worker"]
    result = estimate(arch)
    services = [line.service for line in result.lines]
    assert "Step Functions (Express)" in services
    assert "Lambda" in services
    sf_line = next(line for line in result.lines if line.service == "Step Functions (Express)")
    assert sf_line.monthly_usd <= 0.10
    # Total prototype cost must be cheap (Step Functions ~free + Lambda ~$0.10)
    assert result.total_monthly_usd < 1.00


# ── Item 16: queue_worker pricing ────────────────────────────────────────────


def test_queue_worker_sqs_is_free_at_prototype_scale():
    """Item #16: SQS Standard first 1M requests/mo are free. At prototype
    scale (100k messages) the SQS cost line should be $0. Total for
    queue_worker should be under $1/mo (SQS $0 + Lambda ~$0.10)."""
    from patterns import _CATALOG
    from pricing import estimate

    arch = _CATALOG["queue_worker"]
    result = estimate(arch)
    services = [line.service for line in result.lines]
    assert "SQS" in services
    assert "Lambda" in services
    sqs_line = next(line for line in result.lines if line.service == "SQS")
    assert sqs_line.monthly_usd == 0.00
    # Total must stay cheap
    assert result.total_monthly_usd < 1.00


# ── Item 17: internal_tool pricing ───────────────────────────────────────────


def test_internal_tool_cost_estimate():
    """Item #17: internal_tool has more services than fullstack_with_db --
    internal ALB ($16) + Fargate ($9) + RDS ($12) + WAF ($10) + Secrets
    ($0.40) + Cognito ($0) + Route53 Private Zone ($0.50) = ~$47.90.
    Assert total is between $45 and $55 and that all services have prices."""
    from patterns import _CATALOG
    from pricing import estimate

    arch = _CATALOG["internal_tool"]
    result = estimate(arch)
    # Total in the expected range
    assert result.total_monthly_usd >= 45
    assert result.total_monthly_usd <= 55
    # Cognito is free
    cognito_line = next(line for line in result.lines if line.service == "Cognito User Pool")
    assert cognito_line.monthly_usd == 0.00
    # WAF should be roughly $10
    waf_line = next(line for line in result.lines if line.service == "AWS WAF")
    assert waf_line.monthly_usd >= 8.00
    # Secrets Manager should be ~$0.40
    secrets_line = next(line for line in result.lines if line.service == "Secrets Manager")
    assert secrets_line.monthly_usd <= 1.00
    assert secrets_line.monthly_usd > 0.00


# ── Item 18: live AWS Pricing API (Lambda, Phase 1.5) ────────────────────────

# Helpers -- build a minimal but structurally valid Pricing API response.


def _make_pricing_response(price_usd: str) -> dict:
    """Return a fake get_products response with a single price dimension."""
    product = {
        "product": {"sku": "FAKE123"},
        "terms": {
            "OnDemand": {
                "FAKE123.JRTCKXETXF": {
                    "priceDimensions": {
                        "FAKE123.JRTCKXETXF.6YS6EN2CT7": {
                            "pricePerUnit": {"USD": price_usd},
                            "unit": "Requests",
                        }
                    }
                }
            }
        },
    }
    return {"PriceList": [json.dumps(product)]}


def _make_boto_client(request_price: str = "0.0000002", compute_price: str = "0.0000166667"):
    """Return a MagicMock boto3 client whose get_products alternates between
    the request-price response and the compute-price response."""
    client_mock = MagicMock()
    client_mock.get_products.side_effect = [
        _make_pricing_response(request_price),  # first call: Requests SKU
        _make_pricing_response(compute_price),  # second call: Duration SKU
    ]
    return client_mock


def test_live_lambda_price_used_when_api_responds():
    """Phase 1.5: when the Pricing API returns valid Lambda rates, the Lambda
    cost line note contains 'live' or 'queried' and is_fallback is False."""
    import pricing

    pricing._LIVE_PRICE_CACHE.clear()

    arch = Architecture(
        pattern="node_api",
        services=[ArchitectureService("Lambda", "lambda", {})],
    )

    client_mock = _make_boto_client()
    with patch("pricing.boto3.client", return_value=client_mock):
        result = estimate(arch)

    lambda_line = next(line for line in result.lines if line.service == "Lambda")
    assert "live" in lambda_line.note or "queried" in lambda_line.note
    assert result.is_fallback is False


def test_falls_back_when_pricing_api_raises():
    """Phase 1.5: when the Pricing API raises, the Lambda line still has a
    price (from the static fallback table) and is_fallback is True."""
    import pricing

    pricing._LIVE_PRICE_CACHE.clear()

    arch = Architecture(
        pattern="node_api",
        services=[ArchitectureService("Lambda", "lambda", {})],
    )

    with patch("pricing.boto3.client", side_effect=Exception("no credentials")):
        result = estimate(arch)

    lambda_line = next(line for line in result.lines if line.service == "Lambda")
    # Fallback table has Lambda at $0.10
    assert lambda_line.monthly_usd == 0.10
    assert result.is_fallback is True


def test_falls_back_when_pricing_api_returns_unparseable_response():
    """Phase 1.5: an empty or malformed PriceList triggers the fallback path."""
    import pricing

    pricing._LIVE_PRICE_CACHE.clear()

    arch = Architecture(
        pattern="node_api",
        services=[ArchitectureService("Lambda", "lambda", {})],
    )

    # Return an empty PriceList -- _extract_price_per_unit will return None.
    bad_client = MagicMock()
    bad_client.get_products.return_value = {"PriceList": []}

    with patch("pricing.boto3.client", return_value=bad_client):
        result = estimate(arch)

    lambda_line = next(line for line in result.lines if line.service == "Lambda")
    assert lambda_line.monthly_usd == 0.10
    assert result.is_fallback is True


def test_live_resolver_is_cached_per_process():
    """Phase 1.5: the boto3 client is only created once per process lifetime
    for a given service -- subsequent estimate() calls use the cached result."""
    import pricing

    pricing._LIVE_PRICE_CACHE.clear()

    arch = Architecture(
        pattern="node_api",
        services=[ArchitectureService("Lambda", "lambda", {})],
    )

    client_mock = _make_boto_client()
    with patch("pricing.boto3.client", return_value=client_mock) as boto_patch:
        estimate(arch)
        estimate(arch)  # second call -- should NOT hit boto3.client again

    # boto3.client itself should only have been called once (cache hit on second estimate)
    assert boto_patch.call_count == 1


def test_mixed_live_and_fallback_marks_is_fallback_false():
    """Phase 1.5: when SOME services in an architecture get live prices (e.g.
    S3) and others fall back (e.g. CloudFront, which has no live resolver
    yet), `is_fallback` should be False — at least one line came from the
    live API. Replaces the old static_site-all-fallback test which became
    obsolete once S3 got a live resolver."""
    import pricing

    pricing._LIVE_PRICE_CACHE.clear()

    arch = recommend(RepoProfile(app_type="static_site"))  # S3 + CloudFront

    # Mock boto3 to return a valid S3 price ($0.023/GB-month). S3 is the
    # only service in this architecture with a live resolver, so the mock
    # is called once.
    client_mock = MagicMock()
    client_mock.get_products.return_value = _make_pricing_response("0.023")
    with patch("pricing.boto3.client", return_value=client_mock):
        result = estimate(arch)

    assert result.is_fallback is False  # S3 went live -> not all-fallback
    s3_line = next(line for line in result.lines if line.service == "S3")
    assert "live" in s3_line.note or "queried" in s3_line.note
    # CloudFront stays on the static fallback table.
    cf_line = next(line for line in result.lines if line.service == "CloudFront")
    assert "live" not in cf_line.note and "queried" not in cf_line.note


# ── S3 live pricing ──────────────────────────────────────────────────────────


def test_live_s3_price_used_when_api_responds():
    """When the Pricing API returns a valid S3 storage rate, the S3 cost line
    note contains 'live' or 'queried' and is_fallback is False."""
    import pricing

    pricing._LIVE_PRICE_CACHE.clear()

    arch = Architecture(
        pattern="static_site",
        services=[ArchitectureService("S3", "stores assets", {})],
    )

    client_mock = MagicMock()
    # 1 GB * $0.023/GB-month = $0.023 -> round(0.023, 2) = $0.02
    client_mock.get_products.return_value = _make_pricing_response("0.023")
    with patch("pricing.boto3.client", return_value=client_mock):
        result = estimate(arch)

    s3_line = next(line for line in result.lines if line.service == "S3")
    assert "live" in s3_line.note or "queried" in s3_line.note
    assert s3_line.monthly_usd == 0.02  # round(1.0 * 0.023, 2)
    assert result.is_fallback is False


def test_s3_falls_back_when_pricing_api_raises():
    """When the Pricing API raises, the S3 line still has a price (from the
    static fallback table) and is_fallback is True."""
    import pricing

    pricing._LIVE_PRICE_CACHE.clear()

    arch = Architecture(
        pattern="static_site",
        services=[ArchitectureService("S3", "stores assets", {})],
    )

    # The autouse fixture already makes boto3.client raise -- no need to
    # add our own patch. We just clear the cache and let the default
    # behaviour take over.
    result = estimate(arch)

    s3_line = next(line for line in result.lines if line.service == "S3")
    # Fallback table has S3 at $0.10
    assert s3_line.monthly_usd == 0.10
    assert result.is_fallback is True
