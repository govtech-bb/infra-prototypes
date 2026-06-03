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
