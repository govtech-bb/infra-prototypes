import json
from unittest.mock import patch

import pytest

import deploy_stacks.static_website  # noqa: F401
from deployments import DeploymentStatus, SqliteDeploymentStore


@pytest.fixture
def store(tmp_path):
    return SqliteDeploymentStore(tmp_path / "deploys.db")


@pytest.mark.asyncio
async def test_run_deploy_job_happy_path(store, tmp_path, monkeypatch):
    import jobs_runtime
    from jobs_runtime import run_deploy_job

    monkeypatch.setattr(jobs_runtime, "_STORE", store)
    monkeypatch.setenv("AIBUILDER_DEPLOY_WORKDIR", str(tmp_path / "deploys"))
    monkeypatch.setenv("AIBUILDER_DEPLOY_STATE_BUCKET", "test-bucket")
    monkeypatch.setenv("AIBUILDER_DEPLOY_LOCK_TABLE", "test-lock")

    d = store.create("s", "https://github.com/foo/bar", "static_site", "bar", "proto", ttl_days=14)

    # gh_clone.clone returns (path, error) — synchronous in implementation
    def fake_clone(url, dest):
        path = tmp_path / "clones" / "bar"
        path.mkdir(parents=True, exist_ok=True)
        (path / "index.html").write_text("<html></html>")
        return path, None

    def fake_subprocess(cmd, **kw):
        from subprocess import CompletedProcess

        if "output" in cmd:
            return CompletedProcess(
                cmd,
                0,
                json.dumps(
                    {
                        "bucket_name": {"value": "aibd-bar-proto-static"},
                        "site_url": {"value": "https://d123.cloudfront.net"},
                        "cloudfront_distribution_id": {"value": "E123"},
                    }
                ),
                "",
            )
        return CompletedProcess(cmd, 0, "", "")

    async def fake_sync(*a, **kw):
        return None

    with (
        patch("jobs_runtime.gh_clone.clone", side_effect=fake_clone),
        patch("jobs_runtime.subprocess.run", side_effect=fake_subprocess),
        patch("jobs_runtime.sync_content", side_effect=fake_sync),
    ):
        await run_deploy_job(d.deployment_id)

    loaded = store.get(d.deployment_id)
    assert loaded.status == DeploymentStatus.LIVE
    assert loaded.outputs.get("site_url") == "https://d123.cloudfront.net"


@pytest.mark.asyncio
async def test_run_deploy_job_records_failure_on_clone(store, tmp_path, monkeypatch):
    import jobs_runtime
    from jobs_runtime import run_deploy_job

    monkeypatch.setattr(jobs_runtime, "_STORE", store)
    monkeypatch.setenv("AIBUILDER_DEPLOY_WORKDIR", str(tmp_path / "deploys"))

    d = store.create("s", "https://github.com/foo/bar", "static_site", "bar", "proto", ttl_days=14)

    with patch(
        "jobs_runtime.gh_clone.clone",
        return_value=(None, {"summary": "clone failed", "details": "nope"}),
    ):
        await run_deploy_job(d.deployment_id)

    loaded = store.get(d.deployment_id)
    assert loaded.status == DeploymentStatus.FAILED
    assert "clone failed" in loaded.last_error


@pytest.mark.asyncio
async def test_run_deploy_job_records_failure_on_apply(store, tmp_path, monkeypatch):
    import jobs_runtime
    from jobs_runtime import run_deploy_job

    monkeypatch.setattr(jobs_runtime, "_STORE", store)
    monkeypatch.setenv("AIBUILDER_DEPLOY_WORKDIR", str(tmp_path / "deploys"))
    monkeypatch.setenv("AIBUILDER_DEPLOY_STATE_BUCKET", "test-bucket")
    monkeypatch.setenv("AIBUILDER_DEPLOY_LOCK_TABLE", "test-lock")

    d = store.create("s", "https://github.com/foo/bar", "static_site", "bar", "proto", ttl_days=14)

    def fake_clone(url, dest):
        path = tmp_path / "clones" / "bar"
        path.mkdir(parents=True, exist_ok=True)
        (path / "index.html").write_text("<html></html>")
        return path, None

    def fake_subprocess(cmd, **kw):
        from subprocess import CompletedProcess

        if "init" in cmd:
            return CompletedProcess(cmd, 0, "", "")
        if "apply" in cmd:
            return CompletedProcess(cmd, 1, "", "Error: AccessDenied creating bucket")
        return CompletedProcess(cmd, 0, "", "")

    with (
        patch("jobs_runtime.gh_clone.clone", side_effect=fake_clone),
        patch("jobs_runtime.subprocess.run", side_effect=fake_subprocess),
    ):
        await run_deploy_job(d.deployment_id)

    loaded = store.get(d.deployment_id)
    assert loaded.status == DeploymentStatus.FAILED
    assert "AccessDenied" in (loaded.last_error or "")
