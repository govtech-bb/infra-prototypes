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


# ── UI / static branding ──────────────────────────────────────────────────────


def test_index_page_serves_govtech_branding(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "GovTech" in body
    assert "Barbados" in body
    # Confirms the alpha banner pattern is intact.
    assert "Alpha" in body
    # Confirms the logo is referenced (not the previous emoji-only header).
    assert "govtech-barbados.png" in body


def test_logo_asset_is_served(client):
    r = client.get("/govtech-barbados.png")
    assert r.status_code == 200
    # JPEG starts with FF D8 FF; PNG with 89 50 4E 47. We saved a JPEG with .png
    # extension so accept either signature.
    head = r.content[:4]
    assert head[:3] == b"\xff\xd8\xff" or head == b"\x89PNG"


def test_greeting_lists_all_capabilities(client):
    r = client.get("/")
    body = r.text
    # The opening message advertises every flow the agent supports.
    assert "Deploy" in body and "new prototype" in body
    assert "Update" in body and "existing site" in body
    assert "List" in body and "active deployments" in body
    assert "Destroy" in body and "with confirmation" in body


def test_index_includes_markdown_renderer(client):
    r = client.get("/")
    body = r.text
    # Confirms the DOM-based markdown renderer is wired in. We don't need to
    # invoke it from Python — just ensure the function names exist in source.
    assert "function renderMarkdown" in body
    assert "function renderInline" in body
    assert "function isSafeUrl" in body
    # Sanity: we still construct nodes via createElement / textContent, not by
    # assigning HTML strings to elements (which would be an XSS path with
    # LLM-generated content).
    forbidden = "." + "innerHTML" + " ="
    assert forbidden not in body


def test_typing_indicator_has_bajan_loading_messages(client):
    r = client.get("/")
    body = r.text
    # The pool is named and seeded with a few signature phrases.
    assert "BAJAN_LOADING_MESSAGES" in body
    # A few specific phrases that confirm the pool is non-empty and authored.
    assert "mauby" in body
    assert "Cheese on bread" in body
    assert "Wuh loss" in body
    # The rotation loop is wired in.
    assert "setInterval" in body
    assert "clearInterval" in body
