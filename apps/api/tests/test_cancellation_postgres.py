"""Concurrent HTTP requests against real PostgreSQL locks, in a fresh owned schema."""

import asyncio
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_authorization import create_item, sign_in
from test_iam import policy_data
from test_oidc import oidc_settings
from test_work_cancellation import cancel

from app import main
from app.config import get_settings
from app.db import get_session
from app.iam import OrganizationPolicy, apply_policy
from app.models import AgentEvent, AuditRecord, Base, ResourceLease, WorkerHost, WorkerState
from app.worker_credentials import issue_credential

DATABASE_URL = os.environ.get("KELPIE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="dedicated PostgreSQL test URL not set")


@pytest.fixture
async def cancellation_db():
    schema = f"cancellation_test_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(DATABASE_URL)
    engine = create_async_engine(DATABASE_URL, connect_args={
        "server_settings": {"search_path": schema},
    })
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with sessions() as session:
            yield session

    try:
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        main.app.dependency_overrides[get_session] = override_session
        main.app.dependency_overrides[get_settings] = oidc_settings
        async with sessions() as session:
            await apply_policy(session, OrganizationPolicy.model_validate(policy_data()))
            credential = await issue_credential(session, "cancellation-worker",
                actor="test", reason="isolated PostgreSQL cancellation acceptance")
            worker = await session.get(WorkerHost, credential.worker_id)
            worker.state = WorkerState.ONLINE
            worker.cpu_total = worker.cpu_available = 4
            worker.memory_mb_total = worker.memory_mb_available = 8192
            worker.disk_gb_available = 60
            await session.commit()
        async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test",
                               headers={"Origin": "https://dashboard.example"}) as client:
            await sign_in(client)
            yield client, sessions, credential
    finally:
        main.app.dependency_overrides.clear()
        await engine.dispose()
        # Exact UUID schema owned by this test; retained/public audit rows are never touched.
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin_engine.dispose()


async def claim(client, credential):
    response = await client.post(f"/api/workers/{credential.worker_id}/claim", json={
        "cpu": 2, "memory_mb": 4096, "disk_gb": 30,
    }, headers={"Authorization": f"Bearer {credential.token}"})
    assert response.status_code == 200
    return response.json()


async def assert_waiting(task):
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.1)


async def finish_tasks(*tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_claim_wins_then_waiting_cancellation_reads_committed_assignment(
    cancellation_db, monkeypatch,
):
    client, sessions, credential = cancellation_db
    work = await create_item(client)
    claimed, release = asyncio.Event(), asyncio.Event()
    original = main.claim_next_work

    async def held_claim(*args):
        result = await original(*args)
        claimed.set()
        await release.wait()
        return result

    monkeypatch.setattr(main, "claim_next_work", held_claim)
    claiming = asyncio.create_task(claim(client, credential))
    cancelling = None
    try:
        await asyncio.wait_for(claimed.wait(), timeout=3)
        cancelling = asyncio.create_task(cancel(client, work))
        await assert_waiting(cancelling)
        release.set()
        assert (await asyncio.wait_for(claiming, timeout=3))["work_item"]["id"] == work["id"]
        assert (await asyncio.wait_for(cancelling, timeout=3)).status_code == 409
    finally:
        release.set()
        await finish_tasks(claiming, *([cancelling] if cancelling else []))
    async with sessions() as session:
        assert not list(await session.scalars(select(AuditRecord)))
        lease = await session.scalar(select(ResourceLease))
        assert lease.work_item_id == work["id"] and lease.state == "active"
        worker = await session.get(WorkerHost, credential.worker_id)
        assert (worker.cpu_available, worker.active_runs) == (2, 1)


@pytest.mark.parametrize("rollback", [False, True])
async def test_cancellation_lock_skips_target_without_blocking_another_claim(
    cancellation_db, monkeypatch, rollback,
):
    client, sessions, credential = cancellation_db
    first = await create_item(client)
    second = await create_item(client)
    cancelled, release = asyncio.Event(), asyncio.Event()
    original = main.cancel_queued_work

    async def held_cancel(*args, **kwargs):
        result = await original(*args, **kwargs)
        cancelled.set()
        await release.wait()
        if rollback:
            raise RuntimeError("synthetic cancellation transaction failure")
        return result

    monkeypatch.setattr(main, "cancel_queued_work", held_cancel)
    cancelling = asyncio.create_task(cancel(client, first))
    try:
        await asyncio.wait_for(cancelled.wait(), timeout=3)
        assigned = await asyncio.wait_for(claim(client, credential), timeout=3)
        assert assigned["work_item"]["id"] == second["id"]
        release.set()
        if rollback:
            with pytest.raises(RuntimeError, match="synthetic cancellation"):
                await asyncio.wait_for(cancelling, timeout=3)
            assert (await claim(client, credential))["work_item"]["id"] == first["id"]
        else:
            assert (await asyncio.wait_for(cancelling, timeout=3)).status_code == 200
            assert await claim(client, credential) is None
    finally:
        release.set()
        await finish_tasks(cancelling)
    async with sessions() as session:
        records = list(await session.scalars(select(AuditRecord)))
        assert len(records) == (0 if rollback else 1)
        leases = list(await session.scalars(select(ResourceLease)))
        assert len(leases) == (2 if rollback else 1)
        transitions = list(await session.scalars(select(AgentEvent).where(
            AgentEvent.work_item_id == first["id"], AgentEvent.event_type == "work.transitioned",
        )))
        assert len(transitions) == 1
        assert transitions[0].payload["to"] == ("provisioning" if rollback else "cancelled")


async def test_concurrent_duplicate_cancellation_commits_one_audit(cancellation_db, monkeypatch):
    client, sessions, _ = cancellation_db
    work = await create_item(client)
    cancelled, release = asyncio.Event(), asyncio.Event()
    original = main.cancel_queued_work

    async def held_cancel(*args, **kwargs):
        result = await original(*args, **kwargs)
        cancelled.set()
        await release.wait()
        return result

    monkeypatch.setattr(main, "cancel_queued_work", held_cancel)
    first = asyncio.create_task(cancel(client, work))
    second = None
    try:
        await asyncio.wait_for(cancelled.wait(), timeout=3)
        second = asyncio.create_task(cancel(client, work))
        await assert_waiting(second)
        release.set()
        assert (await asyncio.wait_for(first, timeout=3)).status_code == 200
        assert (await asyncio.wait_for(second, timeout=3)).status_code == 409
    finally:
        release.set()
        await finish_tasks(first, *([second] if second else []))
    async with sessions() as session:
        assert len(list(await session.scalars(select(AuditRecord)))) == 1
        assert len(list(await session.scalars(select(AgentEvent)))) == 2
        assert not list(await session.scalars(select(ResourceLease)))
