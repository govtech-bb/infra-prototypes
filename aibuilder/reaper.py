"""Hourly TTL sweep. Marks expired deployments EXPIRED and enqueues destroy jobs."""

from __future__ import annotations

import asyncio
import logging

from deployments import DeploymentStatus, SqliteDeploymentStore

log = logging.getLogger("aibuilder.reaper")
_INTERVAL_SECONDS = 3600


async def sweep_once(store: SqliteDeploymentStore, queue) -> int:
    expired = store.list_expired()
    for d in expired:
        from jobs_runtime import run_destroy_job  # local import: same-cycle avoidance

        async def _job(did=d.deployment_id):
            await run_destroy_job(did)

        await queue.enqueue(_job)
        d.status = DeploymentStatus.EXPIRED
        store.save(d)
    return len(expired)


async def run_loop(
    store: SqliteDeploymentStore,
    queue,
    *,
    interval: int = _INTERVAL_SECONDS,
) -> None:
    while True:
        try:
            n = await sweep_once(store, queue)
            if n:
                log.info("reaper: enqueued destroy for %d expired deployments", n)
        except Exception:
            log.exception("reaper sweep failed")
        await asyncio.sleep(interval)
