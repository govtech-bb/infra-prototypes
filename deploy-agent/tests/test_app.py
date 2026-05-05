"""Tests for FastAPI route behaviors — focus on the upload-path fix."""

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_AGENT_DB", str(tmp_path / "sessions.db"))
    # Re-import app fresh so the env var is picked up.
    import importlib

    import app as app_module

    importlib.reload(app_module)
    return TestClient(app_module.app)


def _make_session(client) -> str:
    return client.get("/api/session").json()["session_id"]


def test_upload_preserves_nested_path(client):
    session = _make_session(client)
    files = [
        ("files", ("assets/css/main.css", io.BytesIO(b"body{}"), "text/css")),
    ]
    r = client.post(f"/api/upload/{session}", files=files)
    assert r.status_code == 200

    # The file should have landed at the nested location, not flattened.
    upload_dir = Path(f"/tmp/deploy-sessions/{session}")
    assert (upload_dir / "assets/css/main.css").exists()
    assert not (upload_dir / "main.css").exists()


def test_upload_rejects_parent_traversal(client):
    session = _make_session(client)
    files = [
        ("files", ("../etc/passwd", io.BytesIO(b"x"), "text/plain")),
    ]
    r = client.post(f"/api/upload/{session}", files=files)
    assert r.status_code == 400
    assert "Invalid filename" in r.json()["detail"]


def test_upload_rejects_absolute_path(client):
    session = _make_session(client)
    files = [
        ("files", ("/etc/passwd", io.BytesIO(b"x"), "text/plain")),
    ]
    r = client.post(f"/api/upload/{session}", files=files)
    assert r.status_code == 400


def test_upload_flat_filename_still_works(client):
    session = _make_session(client)
    files = [
        ("files", ("index.html", io.BytesIO(b"<html></html>"), "text/html")),
    ]
    r = client.post(f"/api/upload/{session}", files=files)
    assert r.status_code == 200
    upload_dir = Path(f"/tmp/deploy-sessions/{session}")
    assert (upload_dir / "index.html").exists()
