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
    assert [s.aws_service for s in arch.services] == [
        "S3",
        "CloudFront",
        "API Gateway (HTTP API)",
        "Lambda",
    ]
    # SPA-specific notes added in the catalog audit follow-up.
    notes_text = " | ".join(arch.notes)
    assert "OAC" in notes_text or "Origin Access Control" in notes_text
    assert "403" in notes_text and "404" in notes_text  # SPA error handling
    assert "/index.html" in notes_text


def test_static_site_has_oac_note():
    """Audit follow-up: every S3+CloudFront pattern should mention OAC
    (OAI is in maintenance)."""
    profile = RepoProfile(app_type="static_site")
    arch = recommend(profile)
    notes_text = " | ".join(arch.notes)
    assert "OAC" in notes_text or "Origin Access Control" in notes_text


def test_node_api_default_is_lambda():
    """App Runner is being deprecated by AWS — our default for API-shaped
    workloads is now Lambda + API Gateway (true scale-to-zero, cheaper at
    prototype traffic). ECS Fargate is the alternative for long-lived /
    WebSocket use cases."""
    profile = RepoProfile(app_type="node_api")
    arch = recommend(profile)
    assert arch.pattern == "node_api"
    services = [s.aws_service for s in arch.services]
    # HTTP API specifically — REST API is ~3.5x the price and only worth it
    # for usage plans / WAF on the stage / private APIs / etc.
    assert services == ["API Gateway (HTTP API)", "Lambda"]
    # The catalog must never recommend App Runner.
    assert "App Runner" not in services
    assert not any("App Runner" in n for n in arch.notes)
    assert any("Fargate" in note for note in arch.notes)


def test_python_api_default_is_lambda():
    profile = RepoProfile(app_type="python_api")
    arch = recommend(profile)
    services = [s.aws_service for s in arch.services]
    assert services == ["API Gateway (HTTP API)", "Lambda"]
    assert "App Runner" not in services


def test_python_api_promotes_lambda_web_adapter():
    """Audit follow-up: AWS now recommends Lambda Web Adapter (LWA) over
    Mangum for FastAPI/Flask. Notes should lead with LWA; Mangum is the
    fallback option, not the default."""
    profile = RepoProfile(app_type="python_api")
    arch = recommend(profile)
    notes_text = " | ".join(arch.notes)
    assert "Lambda Web Adapter" in notes_text or "LWA" in notes_text
    # LWA should be mentioned before Mangum (the recommended path is first).
    lwa_idx = max(notes_text.find("Lambda Web Adapter"), notes_text.find("LWA"))
    mangum_idx = notes_text.find("Mangum")
    if mangum_idx >= 0:
        assert lwa_idx < mangum_idx, "LWA should appear before Mangum in notes"


def test_dockerized_web_default_routes_to_tiny_container():
    """Audit follow-up #14: dockerized_web now defaults to tiny_container
    (Lambda + LWA + Function URL) instead of ALB + Fargate. Most prototype
    Dockerfiles fit comfortably in Lambda's envelope at ~$0.10/mo vs $25/mo
    for the Fargate path. The Fargate path stays in the catalog under the
    'dockerized_web' key and is reachable via pattern_override."""
    profile = RepoProfile(app_type="dockerized_web")
    arch = recommend(profile)
    assert arch.pattern == "tiny_container"
    services = [s.aws_service for s in arch.services]
    assert services == ["Lambda", "Lambda Function URL"]
    notes_text = " | ".join(arch.notes)
    assert "Lambda Web Adapter" in notes_text or "LWA" in notes_text
    # Notes should mention the Fargate escape hatch so users with long-lived
    # connections / sustained traffic know the alternative exists.
    assert "Fargate" in notes_text


def test_dockerized_web_pattern_override_returns_fargate_path():
    """The original ALB + Fargate path is still in the catalog and remains
    reachable via pattern_override="dockerized_web" for users who need
    always-on containers."""
    from patterns import _CATALOG

    fargate_pattern = _CATALOG["dockerized_web"]
    services = [s.aws_service for s in fargate_pattern.services]
    assert services == ["Application Load Balancer", "ECS Fargate"]


def test_tiny_container_cost_is_tiny():
    """The whole point of tiny_container is that it's cheap at prototype
    scale. Total monthly cost should be under $1/mo (Lambda $0.10 +
    Function URL $0)."""
    arch = recommend(RepoProfile(app_type="dockerized_web"))
    from pricing import estimate

    cost = estimate(arch)
    assert cost.total_monthly_usd < 1.00
    services = [line.service for line in cost.lines]
    assert "Lambda" in services
    assert "Lambda Function URL" in services
    func_url_line = next(line for line in cost.lines if line.service == "Lambda Function URL")
    assert func_url_line.monthly_usd == 0.00


def test_fullstack_with_db_includes_rds_fargate_and_alb():
    """Audit follow-up #12: ALB used to be hand-waved in the Fargate purpose
    string but absent from the services list and the cost. Real production
    needs ALB for TLS termination + stable hostname + health checks +
    target groups (Fargate ENIs rotate on every deploy). Now explicit."""
    profile = RepoProfile(app_type="fullstack_with_db")
    arch = recommend(profile)
    assert arch.pattern == "fullstack_with_db"
    services = [s.aws_service for s in arch.services]
    assert "Application Load Balancer" in services
    assert "ECS Fargate" in services
    assert "RDS PostgreSQL" in services
    assert "App Runner" not in services
    # ALB should be the first service — front-door / traffic-flow order.
    assert services[0] == "Application Load Balancer"


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


# ── Medium-tier audit follow-ups: cross-cutting note coverage ────────────────


def test_custom_domain_note_appears_in_route53_patterns():
    """Audit follow-up: every catalog pattern that uses CloudFront / APIGW /
    ALB should mention the Route53 + ACM custom-domain recipe in its notes.
    Amplify and tiny_container have their own service-specific custom-domain
    notes (different mechanisms) so they're excluded from this check.

    Note: we look at the catalog entries directly (not via recommend()) so
    routing rules don't shift the assertion target — dockerized_web routes
    to tiny_container by default, but the catalog STILL needs the Route53
    note on its underlying `dockerized_web` entry for the pattern_override
    case."""
    from patterns import _CATALOG

    patterns_needing = [
        "static_site",
        "spa_with_api",
        "node_api",
        "python_api",
        "dockerized_web",
        "fullstack_with_db",
    ]
    for pattern_key in patterns_needing:
        arch = _CATALOG[pattern_key]
        notes_text = " | ".join(arch.notes)
        assert "Route53" in notes_text, f"{pattern_key} missing Route53 mention"
        assert "ACM" in notes_text, f"{pattern_key} missing ACM mention"


def test_vpc_baseline_note_appears_in_fargate_patterns():
    """Same as above: look at the catalog directly. dockerized_web's
    underlying Fargate pattern still needs the VPC note for users who
    pattern_override into it."""
    from patterns import _CATALOG

    for pattern_key in ["dockerized_web", "fullstack_with_db"]:
        arch = _CATALOG[pattern_key]
        notes_text = " | ".join(arch.notes)
        assert "VPC" in notes_text, f"{pattern_key} missing VPC note"
        assert "NAT" in notes_text, f"{pattern_key} missing NAT Gateway warning"


def test_cloudwatch_retention_note_in_log_emitting_patterns():
    """Lambda + Fargate + worker patterns all create CloudWatch log groups
    that default to never-expire retention — silent cost trap. The note
    should appear everywhere user code writes logs."""
    patterns_needing = [
        "spa_with_api",
        "node_api",
        "python_api",
        "dockerized_web",
        "fullstack_with_db",
        "worker",
    ]
    for pattern_key in patterns_needing:
        arch = recommend(RepoProfile(app_type=pattern_key))
        notes_text = " | ".join(arch.notes)
        assert "retention" in notes_text.lower(), f"{pattern_key} missing retention note"


def test_fullstack_with_db_has_secrets_management_note():
    """Audit follow-up: fullstack_with_db is the only pattern with a real
    credential (DATABASE_URL). The note should default to Parameter Store
    and explain when to upgrade to Secrets Manager."""
    arch = recommend(RepoProfile(app_type="fullstack_with_db"))
    notes_text = " | ".join(arch.notes)
    assert "Parameter Store" in notes_text
    assert "Secrets Manager" in notes_text  # mentioned as the upgrade
    assert "rotation" in notes_text.lower()  # the trigger for upgrading


def test_fullstack_with_db_warns_about_public_rds():
    """Audit follow-up: catalog used to say nothing about subnet placement.
    Real users were known to put RDS in a public subnet for 'easier dev
    access' — a footgun. The note must explicitly call this out."""
    arch = recommend(RepoProfile(app_type="fullstack_with_db"))
    notes_text = " | ".join(arch.notes)
    assert "private subnet" in notes_text.lower()
    assert "0.0.0.0/0" in notes_text or "never expose" in notes_text.lower()


def test_amplify_mentions_oauth_and_branch_previews():
    """Audit follow-up: Amplify Gen 2 completeness pass — the catalog should
    surface the one-time GitHub OAuth setup AND the branch-preview
    environments (the latter being a key Gen 2 selling point)."""
    profile = RepoProfile(
        app_type="spa_with_api",
        frameworks=["next", "react"],
        has_dockerfile=False,
    )
    arch = recommend(profile)
    notes_text = " | ".join(arch.notes)
    assert "GitHub" in notes_text
    assert "OAuth" in notes_text or "console" in notes_text.lower()
    assert "preview" in notes_text.lower()


def test_amplify_mentions_define_backend_and_sql_data_source():
    """Audit follow-up: previous catalog only mentioned DynamoDB; defineData
    can also back AppSync resolvers against an existing RDS/Aurora SQL
    cluster. Also surface `defineBackend` as the root composition function."""
    profile = RepoProfile(
        app_type="spa_with_api",
        frameworks=["next", "react"],
        has_dockerfile=False,
    )
    arch = recommend(profile)
    notes_text = " | ".join(arch.notes)
    assert "defineBackend" in notes_text
    assert "SQL data source" in notes_text or "RDS" in notes_text


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
