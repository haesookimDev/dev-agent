"""Budget decisions must read the version committed under the real PostgreSQL lock."""

import asyncio

import pytest
from sqlalchemy import select
from test_authorization import create_item
from test_cancellation_postgres import DATABASE_URL, assert_waiting, finish_tasks
from test_cancellation_postgres import cancellation_db as cancellation_db

from app import main
from app.models import AgentEvent, Approval, AuditRecord, WorkItem, WorkStatus

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="dedicated PostgreSQL test URL not set")


async def prepared(client, sessions):
    work = await create_item(client)
    async with sessions() as session:
        (await session.get(WorkItem, work["id"])).status = WorkStatus.BUDGET_EXHAUSTED
        await session.commit()
    return work


async def approve(client, work):
    return await client.post(f"/api/work-items/{work['id']}/approvals", json={
        "kind": "budget", "decision": "approve", "expected_version": work["version"],
        "payload": {"minutes": 15},
    })


@pytest.mark.parametrize("rollback", [False, True])
async def test_budget_confirmation_reads_latest_version_after_waiting_for_lock(
    cancellation_db, rollback,
):
    client, sessions, _ = cancellation_db
    work = await prepared(client, sessions)
    async with sessions() as holding:
        item = await holding.scalar(select(WorkItem).where(
            WorkItem.id == work["id"],
        ).with_for_update())
        # Same exhausted state, but a newer confirmation context in another transaction.
        item.version += 2
        item.budget_minutes += 15
        await holding.flush()
        waiting = asyncio.create_task(approve(client, work))
        try:
            await assert_waiting(waiting)
            await (holding.rollback() if rollback else holding.commit())
            response = await asyncio.wait_for(waiting, timeout=3)
            assert response.status_code == (200 if rollback else 409)
        finally:
            await holding.rollback()
            await finish_tasks(waiting)
    async with sessions() as session:
        item = await session.get(WorkItem, work["id"])
        assert item.budget_minutes == work["budget_minutes"] + 15
        assert item.version == work["version"] + (1 if rollback else 2)
        assert item.status == (WorkStatus.IMPLEMENTING if rollback else WorkStatus.BUDGET_EXHAUSTED)
        for model in (Approval, AuditRecord):
            assert len(list(await session.scalars(select(model)))) == int(rollback)
        assert len(list(await session.scalars(select(AgentEvent)))) == (3 if rollback else 1)


@pytest.mark.parametrize("rollback", [False, True])
async def test_concurrent_budget_confirmations_commit_one_extension(
    cancellation_db, monkeypatch, rollback,
):
    client, sessions, _ = cancellation_db
    work = await prepared(client, sessions)
    auditing, release = asyncio.Event(), asyncio.Event()
    original = main.record_approval_audit

    async def held_audit(*args, **kwargs):
        record = await original(*args, **kwargs)
        if not auditing.is_set():
            auditing.set()
            await release.wait()
            if rollback:
                raise RuntimeError("synthetic budget audit failure")
        return record

    monkeypatch.setattr(main, "record_approval_audit", held_audit)
    first = asyncio.create_task(approve(client, work))
    second = None
    try:
        await asyncio.wait_for(auditing.wait(), timeout=3)
        second = asyncio.create_task(approve(client, work))
        await assert_waiting(second)
        release.set()
        if rollback:
            with pytest.raises(RuntimeError, match="synthetic budget audit failure"):
                await asyncio.wait_for(first, timeout=3)
        else:
            assert (await asyncio.wait_for(first, timeout=3)).status_code == 200
        assert (await asyncio.wait_for(second, timeout=3)).status_code == (200 if rollback else 409)
    finally:
        release.set()
        await finish_tasks(first, *([second] if second else []))
    async with sessions() as session:
        item = await session.get(WorkItem, work["id"])
        assert (item.version, item.budget_minutes, item.status) == (
            work["version"] + 1, work["budget_minutes"] + 15, WorkStatus.IMPLEMENTING,
        )
        for model in (Approval, AuditRecord):
            assert len(list(await session.scalars(select(model)))) == 1
        assert len(list(await session.scalars(select(AgentEvent)))) == 3
