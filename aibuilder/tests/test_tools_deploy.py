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
