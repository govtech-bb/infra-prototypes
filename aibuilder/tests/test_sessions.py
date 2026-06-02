"""Tests for the session store."""

from pathlib import Path

from sessions import Session, SqliteSessionStore


def test_create_returns_session_with_id(tmp_path: Path):
    store = SqliteSessionStore(tmp_path / "test.db")
    session = store.create()
    assert isinstance(session, Session)
    assert session.session_id
    assert session.messages == []
    assert session.clone_path is None
    assert session.last_profile is None


def test_round_trip_through_sqlite(tmp_path: Path):
    store = SqliteSessionStore(tmp_path / "test.db")
    session = store.create()
    session.messages.append({"role": "user", "content": "hi"})
    session.clone_path = "/tmp/repos/xyz/foo"
    session.last_profile = {"app_type": "static_site", "summary": "Static site."}
    store.save(session)

    reloaded = store.get(session.session_id)
    assert reloaded is not None
    assert reloaded.messages == [{"role": "user", "content": "hi"}]
    assert reloaded.clone_path == "/tmp/repos/xyz/foo"
    assert reloaded.last_profile["app_type"] == "static_site"


def test_get_unknown_returns_none(tmp_path: Path):
    store = SqliteSessionStore(tmp_path / "test.db")
    assert store.get("does-not-exist") is None
