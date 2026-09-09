"""Scheduled scan CAS, restart and cancellation using real PostgreSQL commits."""

import asyncio
import os

import pytest
from retention_job_case import checkpoints
from test_artifact_retention_runtime_postgres import assert_waiting
from test_artifact_retention_runtime_postgres import case as case
from test_retention_job import (
    test_checkpoint_moves_past_active_work_and_starts_another_sweep,
    test_concurrent_jobs_journal_once_and_advance_one_version,
    test_crash_after_deletion_retries_without_duplicate_audits,
    test_dry_run_only_changes_job_progress_and_cannot_skip_apply_candidates,
    test_failed_batch_keeps_checkpoint_for_repair_and_retry,
    test_stale_checkpoint_writer_cannot_replace_newer_progress,
)

from app import artifact_retention_job as job
from app import models as m
from app.artifact_retention_worker import schedule

__all__ = [
    "test_checkpoint_moves_past_active_work_and_starts_another_sweep",
    "test_concurrent_jobs_journal_once_and_advance_one_version",
    "test_crash_after_deletion_retries_without_duplicate_audits",
    "test_dry_run_only_changes_job_progress_and_cannot_skip_apply_candidates",
    "test_failed_batch_keeps_checkpoint_for_repair_and_retry",
    "test_stale_checkpoint_writer_cannot_replace_newer_progress",
]
pytestmark = pytest.mark.skipif(not os.environ.get("KELPIE_TEST_POSTGRES_URL"),
                              reason="dedicated PostgreSQL test URL not set")


async def test_stopping_scheduler_joins_blocked_database_job_before_return(case):
    before = await case.snapshot()
    stop, entered = asyncio.Event(), asyncio.Event()
    emitted = []

    async def tick():
        entered.set()
        return await job.run_job(case.sessions, case.root, retain_days=30, apply=True)

    async with case.sessions() as blocker:
        await blocker.get(m.WorkerHost, case.worker, with_for_update=True)
        task = asyncio.create_task(schedule(tick, interval_seconds=300, once=False,
                                            stop=stop, emit=emitted.append))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await assert_waiting(task)
            stop.set()
            assert await asyncio.wait_for(task, 3) == 0
        finally:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not emitted and await case.snapshot() == before
    assert case.path.read_bytes() == case.content
    state = (await checkpoints(case))[0]
    assert state.version == 1 and state.cursor is None
    assert case.sessions.kw["bind"].pool.checkedout() == 0
    result = await job.run_job(case.sessions, case.root, retain_days=30, apply=True)
    assert result["checkpoint_saved"] and result["batch"]["counts"]["purged"] == 1
