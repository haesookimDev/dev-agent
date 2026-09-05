import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from test_delivery_quarantine import pending_delivery as pending_delivery
from test_worker_credentials import database as database

from app import delivery, main
from app.db import SchemaReadiness, SchemaState
from app.models import DeliveryJob, WorkItem, WorkStatus


@pytest.fixture(autouse=True)
def isolate_unrelated_runtime_monitor(monkeypatch):
    monkeypatch.setattr(main, "monitor_runtime_health", AsyncMock())


async def test_recovery_does_not_reset_an_active_delivery(pending_delivery):
    job = pending_delivery
    entered, release = asyncio.Event(), asyncio.Event()

    async def metadata(*_):
        entered.set()
        await release.wait()
        return {"default_branch": "main"}

    job.github.repository.side_effect = metadata
    baseline = asyncio.all_tasks()
    active = asyncio.create_task(delivery.deliver_work(job.work.id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await delivery.resume_pending_deliveries()
        async with job.sessions() as session:
            state = await session.get(DeliveryJob, job.work.id)
            assert state.state == "running"
            assert state.attempts == 1
        job.github.installation_token.assert_awaited_once()
        release.set()
        await asyncio.wait_for(active, 2)
        job.github.create_pull_request.assert_awaited_once()
    finally:
        release.set()
        remaining = asyncio.all_tasks() - baseline
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


@pytest.mark.parametrize("state", ["pending", "retry", "running"])
async def test_recovery_awaits_and_completes_orphaned_jobs(pending_delivery, state):
    job = pending_delivery
    async with job.sessions() as session:
        (await session.get(DeliveryJob, job.work.id)).state = state
        await session.commit()
    await delivery.resume_pending_deliveries()
    async with job.sessions() as session:
        assert (await session.get(WorkItem, job.work.id)).status == WorkStatus.COMPLETED
        record = await session.get(DeliveryJob, job.work.id)
        assert record.state == "completed" and record.attempts == 1
    job.github.create_pull_request.assert_awaited_once()


async def test_recovery_cancellation_joins_its_delivery_children(pending_delivery):
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def stalled(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    pending_delivery.github.repository.side_effect = stalled
    baseline = asyncio.all_tasks()
    recovery = asyncio.create_task(delivery.resume_pending_deliveries())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery
        assert stopped.is_set()
        assert not asyncio.all_tasks() - baseline
    finally:
        remaining = asyncio.all_tasks() - baseline
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


async def test_lifespan_recovers_after_database_becomes_ready(monkeypatch):
    ready = False
    resumed = asyncio.Event()
    readiness = AsyncMock(side_effect=lambda: SchemaReadiness(
        SchemaState.CURRENT if ready else SchemaState.UNREACHABLE,
    ))

    async def resume():
        resumed.set()

    monkeypatch.setattr(main, "get_schema_readiness", readiness)
    monkeypatch.setattr(main, "configure_observability", lambda _: None)
    monkeypatch.setattr(main, "DELIVERY_RECOVERY_RETRY_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(main, "resume_pending_deliveries", resume)
    async with main.lifespan(main.app):
        assert not resumed.is_set()
        ready = True
        await asyncio.wait_for(resumed.wait(), 0.5)
    assert readiness.await_count >= 2


async def test_lifespan_retries_recovery_errors_without_private_logs(monkeypatch, caplog):
    # In-process Alembic tests disable existing loggers through fileConfig.
    monkeypatch.setattr(main.logger, "disabled", False)
    caplog.set_level(logging.WARNING, logger=main.__name__)
    resumed = asyncio.Event()
    calls = 0

    async def resume():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic-private-recovery-connection-string")
        resumed.set()

    monkeypatch.setattr(main, "get_schema_readiness", AsyncMock(
        return_value=SchemaReadiness(SchemaState.CURRENT),
    ))
    monkeypatch.setattr(main, "configure_observability", lambda _: None)
    monkeypatch.setattr(main, "DELIVERY_RECOVERY_RETRY_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(main, "resume_pending_deliveries", resume)
    async with main.lifespan(main.app):
        await asyncio.wait_for(resumed.wait(), 0.5)
    assert calls == 2
    assert "delivery recovery failed; retrying" in caplog.text
    assert "synthetic-private" not in caplog.text


async def test_lifespan_shutdown_cancels_pending_recovery(monkeypatch):
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def resume():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(main, "get_schema_readiness", AsyncMock(
        return_value=SchemaReadiness(SchemaState.CURRENT),
    ))
    monkeypatch.setattr(main, "configure_observability", lambda _: None)
    monkeypatch.setattr(main, "resume_pending_deliveries", resume)
    async with asyncio.timeout(0.5):
        async with main.lifespan(main.app):
            await entered.wait()
    assert stopped.is_set()
