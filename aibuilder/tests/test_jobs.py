# aibuilder/tests/test_jobs.py
import asyncio

import pytest

from jobs import JobQueue


@pytest.mark.asyncio
async def test_enqueue_runs_in_order():
    ran = []

    async def make_job(label):
        async def job():
            ran.append(label)
        return job

    q = JobQueue()
    await q.start()
    await q.enqueue(await make_job("a"))
    await q.enqueue(await make_job("b"))
    await q.drain()
    await q.stop()
    assert ran == ["a", "b"]


@pytest.mark.asyncio
async def test_failing_job_does_not_stop_queue():
    ran = []

    async def boom():
        raise RuntimeError("boom")

    async def ok():
        ran.append("ok")

    q = JobQueue()
    await q.start()
    await q.enqueue(boom)
    await q.enqueue(ok)
    await q.drain()
    await q.stop()
    assert ran == ["ok"]


@pytest.mark.asyncio
async def test_stop_idempotent():
    q = JobQueue()
    await q.start()
    await q.stop()
    await q.stop()  # second stop must not raise
