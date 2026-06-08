"""Tests for the FastAPI endpoints."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-tests")
    # Reset import so the module picks up the patched env vars.
    import importlib

    import app as app_module

    importlib.reload(app_module)
    return TestClient(app_module.app)


def test_health(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_new_session(client: TestClient):
    response = client.get("/api/session")
    assert response.status_code == 200
    assert "session_id" in response.json()


def test_chat_invokes_agent(client: TestClient):
    session_id = client.get("/api/session").json()["session_id"]
    with patch("app.run_agent_loop", return_value="Hello from aibuilder."):
        response = client.post(
            "/api/chat",
            json={"session_id": session_id, "message": "hi"},
        )
    assert response.status_code == 200
    assert response.json()["message"] == "Hello from aibuilder."


def test_chat_unknown_session_returns_404(client: TestClient):
    response = client.post(
        "/api/chat",
        json={"session_id": "does-not-exist", "message": "hi"},
    )
    assert response.status_code == 404


def test_chat_without_token_returns_401_when_token_required(monkeypatch, tmp_path):
    """When AIBUILDER_TOKEN is set in the env, /api/chat must reject
    requests missing or mismatching the Authorization: Bearer header."""
    monkeypatch.setenv("AIBUILDER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-tests")
    monkeypatch.setenv("AIBUILDER_TOKEN", "s3cret")

    import importlib

    import app as app_module

    importlib.reload(app_module)
    client = TestClient(app_module.app)

    sid = client.get("/api/session", headers={"Authorization": "Bearer s3cret"}).json()[
        "session_id"
    ]

    # No header at all → 401
    response = client.post("/api/chat", json={"session_id": sid, "message": "hi"})
    assert response.status_code == 401

    # Wrong token → 401
    response = client.post(
        "/api/chat",
        json={"session_id": sid, "message": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_chat_with_correct_token_passes_middleware(monkeypatch, tmp_path):
    monkeypatch.setenv("AIBUILDER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-tests")
    monkeypatch.setenv("AIBUILDER_TOKEN", "s3cret")

    import importlib

    import app as app_module

    importlib.reload(app_module)
    client = TestClient(app_module.app)

    sid = client.get("/api/session", headers={"Authorization": "Bearer s3cret"}).json()[
        "session_id"
    ]

    with patch("app.run_agent_loop", return_value="ok"):
        response = client.post(
            "/api/chat",
            json={"session_id": sid, "message": "hi"},
            headers={"Authorization": "Bearer s3cret"},
        )
    assert response.status_code == 200


def test_health_endpoint_skips_auth(monkeypatch, tmp_path):
    """The ALB / CloudFront health checks hit /api/health without a token —
    that endpoint MUST stay open regardless of AIBUILDER_TOKEN."""
    monkeypatch.setenv("AIBUILDER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-tests")
    monkeypatch.setenv("AIBUILDER_TOKEN", "s3cret")

    import importlib

    import app as app_module

    importlib.reload(app_module)
    client = TestClient(app_module.app)

    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_no_token_in_env_means_open_for_local_dev(monkeypatch, tmp_path):
    """Local dev convenience: if AIBUILDER_TOKEN is unset, the middleware
    passes through (no auth required). Production always sets the env var."""
    monkeypatch.setenv("AIBUILDER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-tests")
    monkeypatch.delenv("AIBUILDER_TOKEN", raising=False)

    import importlib

    import app as app_module

    importlib.reload(app_module)
    client = TestClient(app_module.app)

    sid = client.get("/api/session").json()["session_id"]
    with patch("app.run_agent_loop", return_value="ok"):
        response = client.post("/api/chat", json={"session_id": sid, "message": "hi"})
    assert response.status_code == 200
