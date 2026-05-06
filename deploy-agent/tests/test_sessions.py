"""Tests for sessions.SqliteSessionStore."""

import pytest

from sessions import SqliteSessionStore


@pytest.fixture
def store(tmp_path):
    return SqliteSessionStore(tmp_path / "sessions.db")


def test_create_returns_session_with_uuid(store):
    s1 = store.create()
    s2 = store.create()
    assert s1.session_id != s2.session_id
    assert len(s1.session_id) == 36  # uuid4
    assert store.get(s1.session_id) is not None


def test_get_unknown_returns_none(store):
    assert store.get("does-not-exist") is None


def test_round_trip_messages_with_mixed_blocks(store):
    s = store.create()
    s.messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hi there"},
                {"type": "tool_use", "id": "tu_1", "name": "deploy", "input": {"x": 1}},
            ],
        },
    ]
    store.save(s)

    loaded = store.get(s.session_id)
    assert loaded is not None
    assert loaded.messages == s.messages


def test_save_persists_files_and_upload_dir(store):
    s = store.create()
    s.files = ["index.html", "style.css"]
    s.upload_dir = "/tmp/foo"
    store.save(s)

    loaded = store.get(s.session_id)
    assert loaded.files == ["index.html", "style.css"]
    assert loaded.upload_dir == "/tmp/foo"


def test_deployment_denormalizes_project_and_env(store, tmp_path):
    s = store.create()
    s.deployment = {
        "bucket_name": "myapp-proto-static",
        "site_url": "https://d123.cloudfront.net",
        "cloudfront_distribution_id": "ABC",
        "project_name": "myapp",
        "env": "proto",
    }
    store.save(s)

    # Inspect the raw row to confirm denormalized columns are populated.
    import sqlite3

    db_path = tmp_path / "sessions.db"
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT project_name, env FROM sessions WHERE session_id = ?",
        (s.session_id,),
    ).fetchone()
    conn.close()
    assert row == ("myapp", "proto")


def test_save_is_idempotent(store):
    s = store.create()
    s.messages = [{"role": "user", "content": "first"}]
    store.save(s)
    s.messages.append({"role": "user", "content": "second"})
    store.save(s)

    loaded = store.get(s.session_id)
    assert len(loaded.messages) == 2


def test_last_injected_file_count_round_trips(store):
    s = store.create()
    assert s.last_injected_file_count == 0
    s.last_injected_file_count = 3
    store.save(s)

    loaded = store.get(s.session_id)
    assert loaded.last_injected_file_count == 3


def test_alter_table_idempotent_on_existing_db(tmp_path):
    SqliteSessionStore(tmp_path / "sessions.db")
    store2 = SqliteSessionStore(tmp_path / "sessions.db")
    s = store2.create()
    s.last_injected_file_count = 5
    store2.save(s)
    assert store2.get(s.session_id).last_injected_file_count == 5
