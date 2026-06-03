"""Tests for cost estimation."""

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
    assert "App Runner" in text
    assert "RDS" in text
    # Services that are NOT in the architecture must NOT contribute assumptions.
    assert "CloudFront" not in text, (
        "fullstack_with_db has no CloudFront — its assumption shouldn't leak through"
    )
    assert "Lambda" not in text, (
        "fullstack_with_db has no Lambda — its assumption shouldn't leak through"
    )

    static = estimate(recommend(RepoProfile(app_type="static_site")))
    static_text = " | ".join(static.assumptions)
    assert "CloudFront" in static_text  # is in static_site arch
    assert "App Runner" not in static_text
    assert "RDS" not in static_text
    assert "Lambda" not in static_text

    spa = estimate(recommend(RepoProfile(app_type="spa_with_api")))
    spa_text = " | ".join(spa.assumptions)
    assert "CloudFront" in spa_text
    assert "Lambda" in spa_text
    assert "App Runner" not in spa_text
    assert "RDS" not in spa_text
