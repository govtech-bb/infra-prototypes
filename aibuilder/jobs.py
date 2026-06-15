# aibuilder/jobs.py
"""In-process async job queue.

A single worker drains an asyncio.Queue of coroutine factories. Deploys
serialize — fine for a small team prototyping. Failures are logged but
never crash the worker. `drain()` is for tests; production lifespan
calls start()/stop() only.

This deliberately stays a module (not a class hierarchy) — chat and the
TTL reaper share the singleton via app.py state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("aibuilder.jobs")


class JobQueue:
    def __init__(self) -> None:
        self._q: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._worker is not None:
            return
        self._stopped.clear()
        self._worker = asyncio.create_task(self._run(), name="aibuilder-job-worker")

    async def enqueue(self, job: Callable[[], Awaitable[None]]) -> None:
        await self._q.put(job)

    async def drain(self) -> None:
        """Block until the queue is empty. For tests only."""
        await self._q.join()

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._stopped.set()
        await self._q.put(None)
        await self._worker
        self._worker = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            job = await self._q.get()
            try:
                if job is None:
                    return
                await job()
            except Exception:
                log.exception("Job failed")
            finally:
                self._q.task_done()
