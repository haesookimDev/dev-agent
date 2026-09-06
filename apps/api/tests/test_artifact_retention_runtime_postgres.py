"""Real retention transactions against UUID-owned PostgreSQL schemas and files."""

import asyncio
import os
import secrets
import uuid
from datetime import timedelta

import pytest
from artifact_retention_case import seed
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_artifact_retention import (
    test_concurrent_alias_cleanup_serializes_and_records_each_phase_once,
    test_crash_boundaries_keep_durable_intent_and_retry_without_duplicate_audits,
    test_final_work_purges_all_aliases_once_and_preserves_execution_resources,
    test_guards_are_reacquired_after_committed_intent_before_unlink,
)

from app import artifact_retention as retention
from app import models as m
from app.auth import hash_token
from app.service import validate_lease
from app.worker_quarantine import quarantine_worker

# Re-run the same behavioral contract using real PostgreSQL commits, not savepoint rollback.
__all__ = [
    "test_concurrent_alias_cleanup_serializes_and_records_each_phase_once",
    "test_crash_boundaries_keep_durable_intent_and_retry_without_duplicate_audits",
    "test_final_work_purges_all_aliases_once_and_preserves_execution_resources",
    "test_guards_are_reacquired_after_committed_intent_before_unlink",
]
DATABASE_URL = os.environ.get("KELPIE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="dedicated PostgreSQL test URL not set")


@pytest.fixture
async def case(tmp_path):
    schema = f"retention_runtime_{uuid.uuid4().hex}"
    owner = create_async_engine(DATABASE_URL)
    async with owner.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(DATABASE_URL, connect_args={
        "server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.run_sync(m.Base.metadata.create_all)
        yield await seed(async_sessionmaker(engine, expire_on_commit=False), tmp_path / "files")
    finally:
        await engine.dispose()
        try:
            # Only the schema whose CREATE above succeeded is ever dropped.
            async with owner.begin() as connection:
                await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await owner.dispose()


async def assert_waiting(task):
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), 0.1)


async def cancel_task(task):
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_retention_waits_for_quarantine_and_rereads_committed_worker_state(case):
    async with case.sessions() as session:
        await quarantine_worker(session, case.worker, actor="synthetic-test",
                                reason="retention serialization")
        task = asyncio.create_task(case.expire())
        try:
            await assert_waiting(task)
            assert case.path.read_bytes() == case.content
            await session.commit()
            result = await asyncio.wait_for(task, 3)
            assert result == retention.RetentionResult("protected", "worker_quarantined_or_missing")
        finally:
            await cancel_task(task)
    rows, audits = await case.evidence()
    assert rows[0].expired_at is None and not audits
    assert case.path.read_bytes() == case.content


@pytest.mark.parametrize("release", [False, True])
async def test_inflight_lease_validation_fences_retention_until_commit(case, release):
    token = secrets.token_urlsafe(32)
    async with case.sessions() as session:
        lease = await session.get(m.ResourceLease, case.lease)
        lease.state, lease.token_hash = "active", hash_token(token)
        lease.expires_at = m.utcnow() + timedelta(minutes=5)
        await session.commit()
    async with case.sessions() as writer:
        lease = await validate_lease(writer, case.work, token, 300)
        task = asyncio.create_task(case.expire())
        try:
            await assert_waiting(task)
            if release:
                lease.state = "released"
            await writer.commit()
            result = await asyncio.wait_for(task, 3)
            assert result.status == ("purged" if release else "protected")
            if not release:
                assert result.reason == "lease_not_released"
        finally:
            await cancel_task(task)
    assert case.path.exists() == (not release)


async def test_quarantine_waits_for_deletion_phase_fences_and_never_interleaves(case, monkeypatch):
    original = retention.locked_group
    locked, proceed = asyncio.Event(), asyncio.Event()
    calls = 0
    async def paused(*args):
        nonlocal calls
        result = await original(*args)
        calls += 1
        if calls == 2:
            locked.set()
            await proceed.wait()
        return result
    async def quarantine():
        async with case.sessions() as session:
            await quarantine_worker(session, case.worker, actor="synthetic-test",
                                    reason="deletion phase serialization")
            await session.commit()
    monkeypatch.setattr(retention, "locked_group", paused)
    deleting = asyncio.create_task(case.expire())
    quarantining = None
    try:
        await asyncio.wait_for(locked.wait(), 3)
        quarantining = asyncio.create_task(quarantine())
        await assert_waiting(quarantining)
        assert case.path.read_bytes() == case.content
        proceed.set()
        assert (await asyncio.wait_for(deleting, 3)).status == "purged"
        await asyncio.wait_for(quarantining, 3)
    finally:
        proceed.set()
        await cancel_task(deleting)
        if quarantining is not None:
            await cancel_task(quarantining)
    rows, audits = await case.evidence()
    assert rows[0].purged_at is not None and len(audits) == 2


async def test_lock_timeout_fails_closed_without_intent_or_file_changes(case):
    before = await case.snapshot()
    async with case.sessions() as blocker:
        await blocker.get(m.WorkerHost, case.worker, with_for_update=True)
        result = await asyncio.wait_for(case.expire(), 4)
        assert result == retention.RetentionResult("failed", "database_unavailable")
    assert await case.snapshot() == before and case.path.read_bytes() == case.content


async def test_cancelled_cli_wait_does_not_leave_deletion_running_in_a_thread(case):
    before = await case.snapshot()
    async with case.sessions() as blocker:
        await blocker.get(m.WorkerHost, case.worker, with_for_update=True)
        task = asyncio.create_task(case.expire())
        try:
            await assert_waiting(task)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await cancel_task(task)
    assert await case.snapshot() == before and case.path.read_bytes() == case.content
    assert (await case.expire()).status == "purged"
