# aibuilder/tests/test_static_website_spec.py
import deploy_stacks.static_website  # noqa: F401  (registers on import)
from deploy_stacks import get_spec
from deployments import Deployment, DeploymentStatus


def _fixture_deployment(**overrides):
    base = dict(
        deployment_id="d1",
        session_id="s",
        repo_url="https://github.com/foo/bar",
        pattern="static_site",
        project_name="bar",
        env="proto",
        status=DeploymentStatus.QUEUED,
        knobs={"is_spa": True, "price_class": "PriceClass_100"},
    )
    base.update(overrides)
    return Deployment(**base)


def test_static_site_pattern_registered():
    assert get_spec("static_site") is not None


def test_build_vars_includes_aibd_prefix_and_knobs():
    spec = get_spec("static_site")
    vars_ = spec.build_vars(_fixture_deployment())
    assert vars_["project_name"].startswith("aibd-")
    assert vars_["env"] == "proto"
    assert vars_["is_spa"] is True
    assert vars_["price_class"] == "PriceClass_100"


def test_allowed_knobs_includes_is_spa_and_price_class():
    spec = get_spec("static_site")
    assert "is_spa" in spec.allowed_knobs
    assert "price_class" in spec.allowed_knobs
