from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from deployments import DeploymentStatus, SqliteDeploymentStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_DEPLOYMENTS_DB", str(tmp_path / "deploys.db"))
    return SqliteDeploymentStore(tmp_path / "deploys.db")


def test_deploy_repo_creates_queued_row_and_enqueues(store, monkeypatch):
    from tools import deploy_repo

    job_queue = MagicMock()
    enqueued = []

    async def fake_enqueue(fn):
        enqueued.append(fn)

    job_queue.enqueue = fake_enqueue
    monkeypatch.setattr("tools._JOB_QUEUE", job_queue)
    monkeypatch.setattr("tools._STORE", store)

    out = deploy_repo(
        github_url="https://github.com/foo/bar",
        pattern="static_site",
        project_name="bar",
        env="proto",
        knobs={"is_spa": True},
        session_id="s1",
        session=MagicMock(),
    )
    assert out["deployment_id"]
    assert out["status"] == "queued"
    listed = store.list_active()
    assert len(listed) == 1
    assert listed[0].status == DeploymentStatus.QUEUED


def test_deploy_repo_rejects_unknown_pattern(store, monkeypatch):
    from tools import deploy_repo

    monkeypatch.setattr("tools._STORE", store)
    out = deploy_repo(
        github_url="https://github.com/foo/bar",
        pattern="worker",
        project_name="bar",
        env="proto",
        knobs={},
        session_id="s1",
        session=MagicMock(),
    )
    assert "summary" in out
    assert "not yet deployable" in out["summary"].lower()


def test_deploy_repo_respects_cap(store, monkeypatch):
    from tools import deploy_repo

    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY", "1")
    monkeypatch.setattr("tools._STORE", store)
    monkeypatch.setattr("tools._JOB_QUEUE", MagicMock(enqueue=lambda fn: None))
    store.create("s1", "u", "static_site", "p", "e", ttl_days=14)
    out = deploy_repo(
        github_url="https://github.com/foo/bar",
        pattern="static_site",
        project_name="bar",
        env="proto",
        knobs={},
        session_id="s1",
        session=MagicMock(),
    )
    assert "session" in out["summary"].lower()


def test_get_deployment_status_returns_row(store, monkeypatch):
    from tools import get_deployment_status

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    out = get_deployment_status(deployment_id=d.deployment_id, session_id="s", session=None)
    assert out["deployment_id"] == d.deployment_id
    assert out["status"] == "queued"


def test_get_deployment_status_404(store, monkeypatch):
    from tools import get_deployment_status

    monkeypatch.setattr("tools._STORE", store)
    out = get_deployment_status(deployment_id="nope", session_id="s", session=None)
    assert "summary" in out


def test_list_deployments_includes_ttl_remaining(store, monkeypatch):
    from tools import list_deployments

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.expires_at = datetime.now(UTC) + timedelta(days=2)
    store.save(d)
    out = list_deployments(session_id="s", session=None)
    rows = out["deployments"]
    assert any(r["deployment_id"] == d.deployment_id for r in rows)
    row = next(r for r in rows if r["deployment_id"] == d.deployment_id)
    assert row["ttl_hours_remaining"] > 0
    assert row["ttl_hours_remaining"] < 100  # ~48h


@pytest.fixture
def mock_queue(monkeypatch):
    q = MagicMock()
    q.enqueue = MagicMock(return_value=None)
    monkeypatch.setattr("tools._JOB_QUEUE", q)
    return q


def test_redeploy_enqueues_for_live_only(store, monkeypatch, mock_queue):
    from tools import redeploy

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    out = redeploy(deployment_id=d.deployment_id, session_id="s", session=None)
    assert "summary" in out  # not live yet — refuse
    d.status = DeploymentStatus.LIVE
    store.save(d)
    out = redeploy(deployment_id=d.deployment_id, session_id="s", session=None)
    assert out["status"] in ("queued", "syncing")


def test_modify_rejects_unknown_knob(store, monkeypatch, mock_queue):
    from tools import modify_deployment

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    store.save(d)
    out = modify_deployment(
        deployment_id=d.deployment_id,
        changes={"haha_not_a_knob": True},
        session_id="s",
        session=None,
    )
    assert "knob" in out["summary"].lower()


def test_modify_accepts_allowed_knob(store, monkeypatch, mock_queue):
    from tools import modify_deployment

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    store.save(d)
    out = modify_deployment(
        deployment_id=d.deployment_id,
        changes={"is_spa": True},
        session_id="s",
        session=None,
    )
    assert out["status"] in ("queued", "modifying")
    loaded = store.get(d.deployment_id)
    assert loaded.knobs["is_spa"] is True


def test_destroy_two_phase(store, monkeypatch, mock_queue):
    from tools import destroy_deployment

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    store.save(d)
    preview = destroy_deployment(deployment_id=d.deployment_id, session_id="s", session=None)
    assert preview["preview"] is True
    confirmed = destroy_deployment(
        deployment_id=d.deployment_id, confirm=True, session_id="s", session=None
    )
    assert confirmed["status"] in ("queued", "destroying")


def test_extend_resets_clock(store, monkeypatch):
    from tools import extend_deployment

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    d.expires_at = datetime.now(UTC) + timedelta(hours=1)
    store.save(d)
    out = extend_deployment(deployment_id=d.deployment_id, session_id="s", session=None)
    assert out["ttl_hours_remaining"] > 200  # 14 days = 336h
