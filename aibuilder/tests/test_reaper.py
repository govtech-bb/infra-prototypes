from datetime import UTC, datetime, timedelta

import pytest

from deployments import DeploymentStatus, SqliteDeploymentStore
from reaper import sweep_once


@pytest.fixture
def store(tmp_path):
    return SqliteDeploymentStore(tmp_path / "deploys.db")


@pytest.mark.asyncio
async def test_sweep_enqueues_destroy_for_expired(store):
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    d.expires_at = datetime.now(UTC) - timedelta(hours=1)
    store.save(d)
    enqueued = []

    async def fake_enqueue(fn):
        enqueued.append(fn)

    class Q:
        enqueue = staticmethod(fake_enqueue)

    n = await sweep_once(store, Q())
    assert n == 1
    assert len(enqueued) == 1
    loaded = store.get(d.deployment_id)
    assert loaded.status == DeploymentStatus.EXPIRED


@pytest.mark.asyncio
async def test_sweep_skips_non_expired(store):
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    store.save(d)

    async def fake_enqueue(fn):
        return None

    class Q:
        enqueue = staticmethod(fake_enqueue)

    n = await sweep_once(store, Q())
    assert n == 0
