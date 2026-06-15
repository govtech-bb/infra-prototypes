from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("AIBUILDER_DEPLOYMENTS_DB", str(tmp_path / "d.db"))
    monkeypatch.delenv("AIBUILDER_TOKEN", raising=False)
    import importlib

    import app

    importlib.reload(app)
    return TestClient(app.app), app


def test_get_deployment_404(client):
    c, _ = client
    r = c.get("/api/deployments/nope")
    assert r.status_code == 404


def test_get_deployment_returns_row(client):
    c, app_mod = client
    d = app_mod.deployment_store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    r = c.get(f"/api/deployments/{d.deployment_id}")
    assert r.status_code == 200
    assert r.json()["deployment_id"] == d.deployment_id


def test_redeploy_endpoint_202_when_live(client):
    c, app_mod = client
    from deployments import DeploymentStatus

    d = app_mod.deployment_store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    app_mod.deployment_store.save(d)
    with patch(
        "app.tools.redeploy", return_value={"deployment_id": d.deployment_id, "status": "queued"}
    ):
        r = c.post(f"/api/deployments/{d.deployment_id}/redeploy")
    assert r.status_code == 202
    assert r.json()["status"] == "queued"


def test_redeploy_endpoint_4xx_when_not_live(client):
    c, app_mod = client
    d = app_mod.deployment_store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    with patch("app.tools.redeploy", return_value={"summary": "not live", "details": ""}):
        r = c.post(f"/api/deployments/{d.deployment_id}/redeploy")
    assert r.status_code == 409
