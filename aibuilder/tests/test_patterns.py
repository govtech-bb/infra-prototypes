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


# ── Amplify Hosting routing for SSR-default frontend frameworks ──────────────
# Amplify Hosting is AWS's Next.js/Nuxt/Remix-native managed host: it handles
# SSR, API routes, middleware, and HTTPS/CDN out of the box, and lets the user
# keep their managed backend (Supabase/Firebase/etc.) without forcing a DB
# migration to RDS. We route the SSR-default frontends here UNLESS the repo
# has a Dockerfile (a strong signal the user wants to BYO container).


def test_next_no_dockerfile_routes_to_amplify_hosting():
    profile = RepoProfile(
        app_type="spa_with_api",
        frameworks=["next", "react"],
        has_dockerfile=False,
    )
    arch = recommend(profile)
    assert arch.pattern == "nextjs_amplify_hosting"
    # Specifically Gen 2 — Gen 1 is in maintenance mode and shouldn't be
    # the default recommendation for new apps.
    assert [s.aws_service for s in arch.services] == ["AWS Amplify (Gen 2)"]
    notes_text = " | ".join(arch.notes)
    assert "Gen 2" in notes_text  # the explicit Gen 1 vs Gen 2 callout
    # Regression: user asked "doesn't Amplify v2 have a database?" — the
    # notes used to imply you HAD to bring an external DB. The bundled
    # AppSync + DynamoDB / Cognito / S3 / Lambda primitives must be called
    # out, and external backends must be framed as opt-in (not required).
    assert "AppSync" in notes_text and "DynamoDB" in notes_text
    assert "opt-in" in notes_text.lower()
    assert "Fargate" in notes_text  # the BYO-Postgres-and-containers alternative


def test_next_with_db_hints_and_no_dockerfile_routes_to_amplify():
    """Real-world scenario: govtech-bb/st-thomas-sign-in is Next.js +
    Supabase + no Dockerfile. The db_hint upgrade would normally route to
    fullstack_with_db (Fargate + RDS) — but the user is keeping Supabase,
    not migrating, so Amplify Hosting is the right answer."""
    profile = RepoProfile(
        app_type="spa_with_api",
        frameworks=["next", "react"],
        has_dockerfile=False,
        has_database_hints=True,
    )
    arch = recommend(profile)
    assert arch.pattern == "nextjs_amplify_hosting"


def test_next_with_dockerfile_stays_on_fargate():
    """A Dockerfile is a strong signal the user wants to BYO container —
    don't override that with the managed-hosting recommendation."""
    profile = RepoProfile(
        app_type="spa_with_api",
        frameworks=["next", "react"],
        has_dockerfile=True,
        has_database_hints=True,
    )
    arch = recommend(profile)
    assert arch.pattern == "fullstack_with_db"
    assert "ECS Fargate" in [s.aws_service for s in arch.services]


def test_nuxt_no_dockerfile_routes_to_amplify():
    profile = RepoProfile(
        app_type="spa_with_api",
        frameworks=["nuxt", "vue"],
        has_dockerfile=False,
    )
    arch = recommend(profile)
    assert arch.pattern == "nextjs_amplify_hosting"


def test_remix_no_dockerfile_routes_to_amplify():
    profile = RepoProfile(
        app_type="spa_with_api",
        frameworks=["react", "remix"],
        has_dockerfile=False,
    )
    arch = recommend(profile)
    assert arch.pattern == "nextjs_amplify_hosting"


def test_express_only_does_not_route_to_amplify():
    """Pattern stays as the existing spa_with_api when no SSR-default
    frontend framework is detected — Amplify Hosting is only the right
    answer for next/nuxt/remix."""
    profile = RepoProfile(
        app_type="spa_with_api",
        frameworks=["react", "express"],
        has_dockerfile=False,
    )
    arch = recommend(profile)
    assert arch.pattern == "spa_with_api"
    assert "Amplify Hosting" not in [s.aws_service for s in arch.services]
