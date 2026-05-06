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
    r = client.get("/api/session")
    assert r.status_code == 200, f"/api/session failed: {r.status_code} {r.text}"
    return r.json()["session_id"]


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


def test_upload_rejects_bare_dotdot(client):
    session = _make_session(client)
    r = client.post(
        f"/api/upload/{session}",
        files=[("files", ("..", io.BytesIO(b"x"), "text/plain"))],
    )
    assert r.status_code == 400


def test_upload_rejects_windows_backslash_traversal(client):
    session = _make_session(client)
    r = client.post(
        f"/api/upload/{session}",
        files=[("files", ("..\\etc\\passwd", io.BytesIO(b"x"), "text/plain"))],
    )
    assert r.status_code == 400


def test_chat_injects_uploaded_files_on_first_turn(client, monkeypatch):
    captured = {}

    def fake_loop(_client, session):
        captured["last_user"] = session.messages[-1]
        return "ok"

    import app as app_module

    monkeypatch.setattr(app_module, "run_agent_loop", fake_loop)

    session = _make_session(client)
    client.post(
        f"/api/upload/{session}",
        files=[("files", ("index.html", io.BytesIO(b"<html></html>"), "text/html"))],
    )
    client.post(
        "/api/chat",
        json={"session_id": session, "message": "deploy"},
    )

    assert captured["last_user"]["role"] == "user"
    assert "[Uploaded files: index.html]" in captured["last_user"]["content"]


def test_chat_re_injects_newly_uploaded_files(client, monkeypatch):
    captured_turns = []

    def fake_loop(_client, session):
        captured_turns.append(session.messages[-1]["content"])
        return "ok"

    import app as app_module

    monkeypatch.setattr(app_module, "run_agent_loop", fake_loop)

    session = _make_session(client)

    client.post(
        f"/api/upload/{session}",
        files=[("files", ("index.html", io.BytesIO(b"<html></html>"), "text/html"))],
    )
    client.post("/api/chat", json={"session_id": session, "message": "first"})

    client.post(
        f"/api/upload/{session}",
        files=[("files", ("style.css", io.BytesIO(b"body{}"), "text/css"))],
    )
    client.post("/api/chat", json={"session_id": session, "message": "second"})

    assert "[Uploaded files: index.html]" in captured_turns[0]
    assert "[Newly uploaded: style.css]" in captured_turns[1]
    assert "index.html" not in captured_turns[1].split("[Newly")[1]


def test_chat_no_injection_when_files_unchanged(client, monkeypatch):
    captured_turns = []

    def fake_loop(_client, session):
        captured_turns.append(session.messages[-1]["content"])
        return "ok"

    import app as app_module

    monkeypatch.setattr(app_module, "run_agent_loop", fake_loop)

    session = _make_session(client)
    client.post(
        f"/api/upload/{session}",
        files=[("files", ("index.html", io.BytesIO(b"<html></html>"), "text/html"))],
    )
    client.post("/api/chat", json={"session_id": session, "message": "first"})
    client.post("/api/chat", json={"session_id": session, "message": "second"})

    assert "[Newly uploaded:" not in captured_turns[1]
    assert "[Uploaded files:" not in captured_turns[1]
    assert captured_turns[1] == "second"
