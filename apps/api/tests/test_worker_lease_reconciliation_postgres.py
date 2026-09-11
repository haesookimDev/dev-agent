"""Real PostgreSQL serialization in an exclusively created test schema."""

import asyncio
import os
import socket
import uuid
from datetime import timedelta

import pytest
import uvicorn
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_api import create_work, register_worker
from test_worker_lease_reconciliation import (
    assigned_lease,
    set_work_status,
    snapshot,
    test_expired_terminal_lease_reconciles_once_and_keeps_audit,
    test_reconciliation_cannot_terminate_or_release_nonterminal_work,
    test_reconciliation_rejects_stale_or_wrong_work,
    test_reconciliation_requires_explicit_strict_cleanup_declaration,
    test_recovery_does_not_bypass_revocation_quarantine_or_individual_auth,
    test_worker_credential_scope_and_lease_ownership_are_both_required,
)

from app import main
from app.config import Settings, get_settings
from app.db import get_session
from app.models import AuditRecord, Base, DeliveryJob, ResourceLease, WorkItem, WorkStatus, utcnow
from app.worker_credentials import issue_credential, revoke_credential
from app.worker_quarantine import quarantine_worker

__all__ = [
    "assigned_lease",
    "test_expired_terminal_lease_reconciles_once_and_keeps_audit",
    "test_reconciliation_cannot_terminate_or_release_nonterminal_work",
    "test_reconciliation_rejects_stale_or_wrong_work",
    "test_reconciliation_requires_explicit_strict_cleanup_declaration",
    "test_recovery_does_not_bypass_revocation_quarantine_or_individual_auth",
    "test_worker_credential_scope_and_lease_ownership_are_both_required",
]
DATABASE_URL = os.environ.get("KELPIE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="dedicated PostgreSQL test URL not set")


@pytest.fixture
async def client(tmp_path):
    schema = f"lease_recovery_{uuid.uuid4().hex}"
    owner = create_async_engine(DATABASE_URL)
    engine = create_async_engine(DATABASE_URL, connect_args={
        "server_settings": {"search_path": schema},
    })
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    created = False

    async def override_session():
        async with sessions() as session:
            yield session

    try:
        async with owner.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        created = True
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        main.app.dependency_overrides[get_session] = override_session
        main.app.dependency_overrides[get_settings] = lambda: Settings(
            artifact_root=str(tmp_path / "artifacts"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=main.app), base_url="http://test",
        ) as http:
            yield http
    finally:
        main.app.dependency_overrides.clear()
        await engine.dispose()
        try:
            if created:
                # Never disable the append-only audit trigger on retained/user data.
                await drop_owned_schema(owner, schema)
        finally:
            await owner.dispose()


async def drop_owned_schema(owner, schema):
    async with owner.begin() as connection:
        await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')


async def finish_tasks(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)


async def assert_waiting(task):
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.1)


async def test_duplicate_reconciliation_serializes_and_other_worker_keeps_running(
    client, worker_headers, assigned_lease, monkeypatch,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    held, proceed = asyncio.Event(), asyncio.Event()
    original = main.reconcile_terminal_lease

    async def hold(*args):
        await original(*args)
        held.set()
        await proceed.wait()

    monkeypatch.setattr(main, "reconcile_terminal_lease", hold)
    first = asyncio.create_task(client.post(
        path + "/reconcile", headers=worker_headers, json=payload,
    ))
    second = None
    try:
        await asyncio.wait_for(held.wait(), 3)
        second = asyncio.create_task(client.post(
            path + "/reconcile", headers=worker_headers, json=payload,
        ))
        await assert_waiting(second)
        async for session in main.app.dependency_overrides[get_session]():
            other = await asyncio.wait_for(issue_credential(
                session, "independent-worker", actor="test", reason="independent lock proof",
            ), 3)
            await session.commit()
        independent = await asyncio.wait_for(client.post("/api/workers/register", headers={
            "Authorization": f"Bearer {other.token}",
        }, json={"name": "independent-worker", "cpu_total": 2, "memory_mb_total": 4096,
                 "disk_gb_available": 30}), 3)
        assert independent.status_code == 200
        proceed.set()
        assert (await asyncio.wait_for(first, 3)).status_code == 204
        assert (await asyncio.wait_for(second, 3)).status_code == 204
    finally:
        proceed.set()
        await finish_tasks(first, second)
    assert await snapshot(worker["id"], claim["lease_id"]) == (8, 16384, 500, 0, "released", 1, 1)
    async for session in main.app.dependency_overrides[get_session]():
        audit = await session.scalar(select(AuditRecord).where(
            AuditRecord.target_id == claim["lease_id"], AuditRecord.action == "lease.reconciled",
        ))
        assert audit.details["lease_state_before"] == "active"


@pytest.mark.parametrize("operation", ["revoke", "quarantine"])
async def test_waiting_reconciliation_rereads_committed_credential_denial(
    client, worker_headers, assigned_lease, operation,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    async for session in main.app.dependency_overrides[get_session]():
        if operation == "revoke":
            credential = worker_headers["Authorization"].removeprefix("Bearer kwc_").split(".")[0]
            await revoke_credential(session, credential, actor="test", reason="concurrent revoke")
        else:
            await quarantine_worker(
                session, worker["id"], actor="test", reason="concurrent quarantine",
            )
        reconciling = asyncio.create_task(client.post(
            path + "/reconcile", headers=worker_headers, json=payload,
        ))
        try:
            await assert_waiting(reconciling)
            await session.commit()
            assert (await asyncio.wait_for(reconciling, 3)).status_code == 401
        finally:
            await finish_tasks(reconciling)
    lease_state = "quarantined" if operation == "quarantine" else "active"
    assert await snapshot(worker["id"], claim["lease_id"]) == (6, 12288, 470, 1, lease_state, 0, 0)


@pytest.mark.parametrize("rollback", [False, True])
async def test_existing_release_and_reconciliation_share_one_resource_transaction(
    client, worker_headers, assigned_lease, monkeypatch, rollback,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    held, proceed = asyncio.Event(), asyncio.Event()
    original = main.emit_event

    async def hold(*args, **kwargs):
        event = await original(*args, **kwargs)
        if event.event_type == "lease.released":
            held.set()
            await proceed.wait()
            if rollback:
                raise RuntimeError("synthetic release rollback")
        return event

    monkeypatch.setattr(main, "emit_event", hold)
    releasing = asyncio.create_task(client.post(
        f"/api/runs/{payload['work_item_id']}/release",
        headers={"X-Kelpie-Lease": claim["lease_token"]},
    ))
    reconciling = None
    try:
        await asyncio.wait_for(held.wait(), 3)
        reconciling = asyncio.create_task(client.post(
            path + "/reconcile", headers=worker_headers, json=payload,
        ))
        await assert_waiting(reconciling)
        proceed.set()
        if rollback:
            with pytest.raises(RuntimeError, match="synthetic release rollback"):
                await asyncio.wait_for(releasing, 3)
        else:
            assert (await asyncio.wait_for(releasing, 3)).status_code == 204
        assert (await asyncio.wait_for(reconciling, 3)).status_code == 204
    finally:
        proceed.set()
        await finish_tasks(releasing, reconciling)
    assert await snapshot(worker["id"], claim["lease_id"]) == (
        8, 16384, 500, 0, "released", 1, 1,
    )
    # Both paths record the later declaration once, without another release event.
    for _ in range(2):
        response = await client.post(path + "/reconcile", headers=worker_headers, json=payload)
        assert response.status_code == 204
    assert await snapshot(worker["id"], claim["lease_id"]) == (8, 16384, 500, 0, "released", 1, 1)
    async for session in main.app.dependency_overrides[get_session]():
        audit = await session.scalar(select(AuditRecord).where(
            AuditRecord.target_id == claim["lease_id"], AuditRecord.action == "lease.reconciled",
        ))
        assert audit.details["lease_state_before"] == ("active" if rollback else "released")


async def test_reconciliation_rollback_keeps_resources_and_audit_atomic(
    client, worker_headers, assigned_lease, monkeypatch,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    original = main.reconcile_terminal_lease

    async def fail(*args):
        await original(*args)
        raise RuntimeError("synthetic reconciliation rollback")

    monkeypatch.setattr(main, "reconcile_terminal_lease", fail)
    with pytest.raises(RuntimeError, match="synthetic reconciliation rollback"):
        await client.post(path + "/reconcile", headers=worker_headers, json=payload)
    assert await snapshot(worker["id"], claim["lease_id"]) == (6, 12288, 470, 1, "active", 0, 0)
    monkeypatch.setattr(main, "reconcile_terminal_lease", original)
    response = await client.post(path + "/reconcile", headers=worker_headers, json=payload)
    assert response.status_code == 204
    assert await snapshot(worker["id"], claim["lease_id"]) == (8, 16384, 500, 0, "released", 1, 1)


async def test_claim_and_reconciliation_account_for_each_owned_lease_once(
    client, worker_headers, assigned_lease,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    await create_work(client, "Next independent queued task")
    responses = await asyncio.gather(
        client.post(path + "/reconcile", headers=worker_headers, json=payload),
        client.post(f"/api/workers/{worker['id']}/claim", headers=worker_headers, json={}),
    )
    assert responses[0].status_code == 204
    assert responses[1].status_code == 200
    next_claim = responses[1].json()
    assert next_claim["lease_id"] != claim["lease_id"]
    assert await snapshot(worker["id"], claim["lease_id"]) == (6, 12288, 470, 1, "released", 1, 1)
    async for session in main.app.dependency_overrides[get_session]():
        leases = list((await session.scalars(select(ResourceLease))).all())
        assert len(leases) == 2
        assert sum(lease.state == "active" for lease in leases) == 1


@pytest.mark.parametrize("state", ["pending", "retry", "running", "unknown-future", "quarantined"])
async def test_terminal_work_with_active_delivery_still_cannot_release(
    client, worker_headers, assigned_lease, state,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    async for session in main.app.dependency_overrides[get_session]():
        session.add(DeliveryJob(work_item_id=payload["work_item_id"], state=state))
        await session.commit()
    response = await client.post(path + "/reconcile", headers=worker_headers, json=payload)
    assert response.status_code == 409
    assert await snapshot(worker["id"], claim["lease_id"]) == (6, 12288, 470, 1, "active", 0, 0)


@pytest.mark.parametrize("state", ["completed", "failed"])
async def test_known_finished_delivery_permits_terminal_reconciliation(
    client, worker_headers, assigned_lease, state,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    async for session in main.app.dependency_overrides[get_session]():
        session.add(DeliveryJob(work_item_id=payload["work_item_id"], state=state))
        await session.commit()
    response = await client.post(path + "/reconcile", headers=worker_headers, json=payload)
    assert response.status_code == 204
    assert await snapshot(worker["id"], claim["lease_id"]) == (8, 16384, 500, 0, "released", 1, 1)


async def test_reopened_lease_with_reconciliation_history_fails_closed(
    client, worker_headers, assigned_lease,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    response = await client.post(path + "/reconcile", headers=worker_headers, json=payload)
    assert response.status_code == 204
    async for session in main.app.dependency_overrides[get_session]():
        lease = await session.get(ResourceLease, claim["lease_id"])
        lease.state = "active"  # Simulate inconsistent/restored state; do not invent a new lease.
        await session.commit()
    before = await snapshot(worker["id"], claim["lease_id"])
    response = await client.post(path + "/reconcile", headers=worker_headers, json=payload)
    assert response.status_code == 409
    assert await snapshot(worker["id"], claim["lease_id"]) == before


@pytest.mark.parametrize("invalid", ["assignment", "lease_state", "missing_cleanup"])
async def test_inconsistent_assignment_or_missing_cleanup_fails_closed(
    client, worker_headers, assigned_lease, invalid,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    async for session in main.app.dependency_overrides[get_session]():
        if invalid == "assignment":
            item = await session.get(WorkItem, payload["work_item_id"])
            item.assigned_worker_id = None
        elif invalid == "lease_state":
            lease = await session.get(ResourceLease, claim["lease_id"])
            lease.state = "quarantined"
        else:
            del payload["cleanup_confirmed"]
        await session.commit()
    before = await snapshot(worker["id"], claim["lease_id"])
    response = await client.post(path + "/reconcile", headers=worker_headers, json=payload)
    assert response.status_code == (422 if invalid == "missing_cleanup" else 409)
    assert await snapshot(worker["id"], claim["lease_id"]) == before


async def test_actual_loopback_http_recovery_uses_postgres_and_never_duplicates_capacity(
    client, worker_headers,
):
    # Real TCP + PostgreSQL. Lifespan disabled: the schema-scoped fixture is not
    # a production startup/readiness test and does not start background jobs.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        main.app, log_config=None, access_log=False, lifespan="off", log_level="critical",
    ))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("HTTP server exited before startup")
                await asyncio.sleep(0.01)
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as http:
            work = await create_work(http)
            worker = await register_worker(http, worker_headers)
            claimed = await http.post(
                f"/api/workers/{worker['id']}/claim", headers=worker_headers, json={},
            )
            assert claimed.status_code == 200
            claim = claimed.json()
            run_headers = {"X-Kelpie-Lease": claim["lease_token"]}
            transitioned = await http.post(
                f"/api/runs/{work['id']}/transition", headers=run_headers,
                json={"status": "failed", "expected_version": 2},
            )
            assert transitioned.status_code == 200
            async for session in main.app.dependency_overrides[get_session]():
                lease = await session.get(ResourceLease, claim["lease_id"])
                lease.expires_at = utcnow() - timedelta(seconds=1)
                await session.commit()
            path = f"/api/workers/{worker['id']}/leases/{claim['lease_id']}"
            view = await http.get(path, headers=worker_headers)
            assert view.status_code == 200
            payload = {"work_item_id": work["id"], "expected_version": view.json()["work_version"],
                       "cleanup_confirmed": True}
            assert payload["expected_version"] == 3
            denied = await http.post(path + "/reconcile", headers=run_headers, json=payload)
            assert denied.status_code == 401
            stale = await http.post(path + "/reconcile", headers=worker_headers,
                                    json=payload | {"expected_version": 2})
            assert stale.status_code == 409
            responses = await asyncio.gather(*(
                http.post(path + "/reconcile", headers=worker_headers, json=payload)
                for _ in range(2)
            ))
            assert [response.status_code for response in responses] == [204, 204]
            assert await snapshot(worker["id"], claim["lease_id"]) == (
                8, 16384, 500, 0, "released", 1, 1,
            )
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(serving), timeout=3)
        finally:
            await finish_tasks(serving)
            listener.close()
