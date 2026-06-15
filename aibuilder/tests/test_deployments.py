from datetime import UTC, datetime, timedelta

import pytest

from deployments import DeploymentStatus, SqliteDeploymentStore


@pytest.fixture
def store(tmp_path):
    return SqliteDeploymentStore(tmp_path / "deploys.db")


def test_create_returns_deployment_with_id_and_queued_status(store):
    d = store.create(
        session_id="s1",
        repo_url="https://github.com/foo/bar",
        pattern="static_site",
        project_name="bar",
        env="proto",
        ttl_days=14,
    )
    assert d.deployment_id
    assert d.status == DeploymentStatus.QUEUED
    assert d.session_id == "s1"
    assert d.expires_at > datetime.now(UTC)


def test_get_returns_none_for_missing(store):
    assert store.get("nonexistent") is None


def test_save_then_get_roundtrips(store):
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    d.outputs = {"site_url": "https://example.com"}
    store.save(d)
    loaded = store.get(d.deployment_id)
    assert loaded.status == DeploymentStatus.LIVE
    assert loaded.outputs == {"site_url": "https://example.com"}


def test_list_active_excludes_destroyed(store):
    a = store.create("s", "u", "static_site", "a", "e", ttl_days=14)
    b = store.create("s", "u", "static_site", "b", "e", ttl_days=14)
    b.status = DeploymentStatus.DESTROYED
    store.save(b)
    active = store.list_active()
    ids = [d.deployment_id for d in active]
    assert a.deployment_id in ids
    assert b.deployment_id not in ids


def test_list_for_session_filters(store):
    a = store.create("s1", "u", "static_site", "a", "e", ttl_days=14)
    store.create("s2", "u", "static_site", "b", "e", ttl_days=14)
    out = store.list_for_session("s1")
    assert len(out) == 1
    assert out[0].deployment_id == a.deployment_id


def test_count_today_for_session_counts_only_today(store):
    store.create("s", "u", "static_site", "a", "e", ttl_days=14)
    store.create("s", "u", "static_site", "b", "e", ttl_days=14)
    store.create("other", "u", "static_site", "c", "e", ttl_days=14)
    assert store.count_today_for_session("s") == 2
    assert store.count_today_global() == 3


def test_list_expired_returns_past_ttl(store):
    d = store.create("s", "u", "static_site", "a", "e", ttl_days=14)
    d.expires_at = datetime.now(UTC) - timedelta(hours=1)
    d.status = DeploymentStatus.LIVE
    store.save(d)
    expired = store.list_expired()
    assert len(expired) == 1


def test_extend_resets_clock(store):
    d = store.create("s", "u", "static_site", "a", "e", ttl_days=14)
    original = d.expires_at
    d.expires_at = datetime.now(UTC) + timedelta(days=1)
    store.save(d)
    d = store.extend(d.deployment_id, days=14)
    assert d.expires_at > original


def test_recover_in_flight_marks_them_failed(store):
    d1 = store.create("s", "u", "static_site", "a", "e", ttl_days=14)
    d2 = store.create("s", "u", "static_site", "b", "e", ttl_days=14)
    d2.status = DeploymentStatus.APPLYING
    store.save(d2)
    n = store.recover_in_flight()
    assert n == 2
    loaded = store.get(d1.deployment_id)
    assert loaded.status == DeploymentStatus.FAILED
    assert "interrupted" in loaded.last_error.lower()


def test_schema_migration_is_idempotent(tmp_path):
    SqliteDeploymentStore(tmp_path / "x.db")
    SqliteDeploymentStore(tmp_path / "x.db")  # second open mustn't crash
