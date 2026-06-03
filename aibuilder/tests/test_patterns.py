"""Tests for the AWS architecture pattern catalog."""

from analyzer import RepoProfile
from patterns import Architecture, recommend


def test_static_site_pattern():
    profile = RepoProfile(app_type="static_site")
    arch = recommend(profile)
    assert isinstance(arch, Architecture)
    assert arch.pattern == "static_site"
    services = [s.aws_service for s in arch.services]
    assert services == ["S3", "CloudFront"]


def test_spa_with_api_pattern():
    profile = RepoProfile(app_type="spa_with_api")
    arch = recommend(profile)
    assert arch.pattern == "spa_with_api"
    assert [s.aws_service for s in arch.services] == ["S3", "CloudFront", "API Gateway", "Lambda"]


def test_node_api_default_is_lambda():
    """App Runner is being deprecated by AWS — our default for API-shaped
    workloads is now Lambda + API Gateway (true scale-to-zero, cheaper at
    prototype traffic). ECS Fargate is the alternative for long-lived /
    WebSocket use cases."""
    profile = RepoProfile(app_type="node_api")
    arch = recommend(profile)
    assert arch.pattern == "node_api"
    services = [s.aws_service for s in arch.services]
    assert services == ["API Gateway", "Lambda"]
    # The catalog must never recommend App Runner.
    assert "App Runner" not in services
    assert not any("App Runner" in n for n in arch.notes)
    assert any("Fargate" in note for note in arch.notes)


def test_python_api_default_is_lambda():
    profile = RepoProfile(app_type="python_api")
    arch = recommend(profile)
    services = [s.aws_service for s in arch.services]
    assert services == ["API Gateway", "Lambda"]
    assert "App Runner" not in services


def test_dockerized_web_default_is_fargate():
    profile = RepoProfile(app_type="dockerized_web")
    arch = recommend(profile)
    services = [s.aws_service for s in arch.services]
    assert services == ["ECS Fargate"]
    assert "App Runner" not in services
    # Notes should still mention the simpler-config alternative.
    assert any("ECS Express" in n for n in arch.notes)


def test_fullstack_with_db_includes_rds_and_fargate():
    profile = RepoProfile(app_type="fullstack_with_db")
    arch = recommend(profile)
    assert arch.pattern == "fullstack_with_db"
    services = [s.aws_service for s in arch.services]
    assert "ECS Fargate" in services
    assert "RDS PostgreSQL" in services
    assert "App Runner" not in services


def test_spa_with_api_upgrades_to_fullstack_when_db_hints():
    profile = RepoProfile(app_type="spa_with_api", has_database_hints=True)
    arch = recommend(profile)
    assert arch.pattern == "fullstack_with_db"


def test_worker_pattern():
    profile = RepoProfile(app_type="worker")
    arch = recommend(profile)
    assert arch.pattern == "worker"
    services = [s.aws_service for s in arch.services]
    assert services == ["EventBridge Scheduler", "Lambda"]


def test_unknown_returns_empty_with_helpful_note():
    profile = RepoProfile(app_type="unknown")
    arch = recommend(profile)
    assert arch.pattern == "unknown"
    assert arch.services == []
    assert any("describe" in n.lower() for n in arch.notes)
