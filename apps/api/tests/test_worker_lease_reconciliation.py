"""Restart reconciliation never turns an identifier into a credential."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_api import create_work, register_worker

from app.config import Settings, get_settings
from app.db import get_session
from app.main import app
from app.models import (
    AgentEvent,
    AuditRecord,
    ResourceLease,
    WorkerHost,
    WorkItem,
    WorkStatus,
    utcnow,
)
from app.worker_credentials import issue_credential, revoke_credential
from app.worker_quarantine import quarantine_worker


@pytest.fixture
async def assigned_lease(client, worker_headers):
    work = await create_work(client)
    worker = await register_worker(client, worker_headers)
    claimed = await client.post(
        f"/api/workers/{worker['id']}/claim", headers=worker_headers, json={},
    )
    assert claimed.status_code == 200
    claim = claimed.json()
    path = f"/api/workers/{worker['id']}/leases/{claim['lease_id']}"
    payload = {"work_item_id": work["id"], "expected_version": 2, "cleanup_confirmed": True}
    return worker, claim, path, payload


async def set_work_status(claim, status):
    async for session in app.dependency_overrides[get_session]():
        item = await session.get(WorkItem, claim["work_item"]["id"])
        item.status = status
        await session.commit()


async def snapshot(worker_id, lease_id):
    async for session in app.dependency_overrides[get_session]():
        worker = await session.get(WorkerHost, worker_id)
        lease = await session.get(ResourceLease, lease_id)
        events = list((await session.scalars(select(AgentEvent).where(
            AgentEvent.work_item_id == lease.work_item_id,
            AgentEvent.event_type == "lease.released",
        ))).all())
        audits = list((await session.scalars(select(AuditRecord).where(
            AuditRecord.target_id == lease.id, AuditRecord.action == "lease.reconciled",
        ))).all())
        return (worker.cpu_available, worker.memory_mb_available, worker.disk_gb_available,
                worker.active_runs, lease.state, len(events), len(audits))


@pytest.mark.parametrize("terminal", [
    WorkStatus.FAILED, WorkStatus.CANCELLED, WorkStatus.COMPLETED,
])
async def test_expired_terminal_lease_reconciles_once_and_keeps_audit(
    client, worker_headers, assigned_lease, terminal,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, terminal)
    async for session in app.dependency_overrides[get_session]():
        lease = await session.get(ResourceLease, claim["lease_id"])
        lease.expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()
    original_release = await client.post(
        f"/api/runs/{payload['work_item_id']}/release",
        headers={"X-Kelpie-Lease": claim["lease_token"]},
    )
    assert original_release.status_code == 401
    inspected = await client.get(path, headers=worker_headers)
    assert inspected.status_code == 200
    assert inspected.json() == {
        "lease_id": claim["lease_id"], "work_item_id": payload["work_item_id"],
        "worker_id": worker["id"], "state": "active", "work_status": terminal.value,
        "work_version": 2, "cpu": 2, "memory_mb": 4096, "disk_gb": 30,
    }
    for _ in range(2):  # A lost response must not return disk/capacity twice.
        response = await client.post(path + "/reconcile", headers=worker_headers, json=payload)
        assert response.status_code == 204, response.text
        assert await snapshot(worker["id"], claim["lease_id"]) == (
            8, 16384, 500, 0, "released", 1, 1,
        )
    async for session in app.dependency_overrides[get_session]():
        audit = await session.scalar(select(AuditRecord).where(
            AuditRecord.target_id == claim["lease_id"],
        ))
        assert audit.actor_subject == f"worker:{worker['id']}"
        assert audit.identity_provider == "worker-credential"
        assert audit.transport == "background"
        assert audit.details["cleanup_confirmed"] is True
        assert audit.details["work_version"] == 2
        assert audit.details["work_status"] == terminal.value
        assert audit.details["lease_state_before"] == "active"
        assert audit.details["lease_state_after"] == "released"
        assert audit.correlation_id == claim["work_item"]["correlation_id"]
        assert claim["lease_token"] not in str(audit.details)
        assert worker_headers["Authorization"] not in str(audit.details)


@pytest.mark.parametrize("state", [state for state in WorkStatus if state not in {
    WorkStatus.FAILED, WorkStatus.CANCELLED, WorkStatus.COMPLETED,
}])
async def test_reconciliation_cannot_terminate_or_release_nonterminal_work(
    client, worker_headers, assigned_lease, state,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, state)
    before = await snapshot(worker["id"], claim["lease_id"])
    response = await client.post(path + "/reconcile", headers=worker_headers, json=payload)
    assert response.status_code == 409
    assert await snapshot(worker["id"], claim["lease_id"]) == before


@pytest.mark.parametrize("change", [
    {"expected_version": 1}, {"work_item_id": str(uuid.uuid4())},
])
async def test_reconciliation_rejects_stale_or_wrong_work(
    client, worker_headers, assigned_lease, change,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    before = await snapshot(worker["id"], claim["lease_id"])
    response = await client.post(path + "/reconcile", headers=worker_headers, json=payload | change)
    assert response.status_code == 409
    assert await snapshot(worker["id"], claim["lease_id"]) == before


@pytest.mark.parametrize("change", [
    {"cleanup_confirmed": False}, {"cleanup_confirmed": "true"}, {"cleanup_confirmed": 1},
    {"expected_version": True}, {"expected_version": "2"}, {"work_item_id": "invalid"},
    {"lease_token": "not-accepted-here"},
])
async def test_reconciliation_requires_explicit_strict_cleanup_declaration(
    client, worker_headers, assigned_lease, change,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    before = await snapshot(worker["id"], claim["lease_id"])
    response = await client.post(path + "/reconcile", headers=worker_headers, json=payload | change)
    assert response.status_code == 422
    assert await snapshot(worker["id"], claim["lease_id"]) == before


async def test_worker_credential_scope_and_lease_ownership_are_both_required(
    client, worker_headers, assigned_lease,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    async for session in app.dependency_overrides[get_session]():
        other = await issue_credential(session, "other-worker", actor="test", reason="scope test")
        await session.commit()
    for candidate, headers, expected in [
        (path, {}, 401),
        (path, {"X-Kelpie-Lease": claim["lease_token"]}, 401),
        (path, {"Authorization": f"Bearer {claim['lease_id']}"}, 401),
        (path, {"Authorization": f"Bearer {other.token}"}, 403),
        (path.replace(worker["id"], other.worker_id), worker_headers, 403),
        (path.replace(worker["id"], other.worker_id),
         {"Authorization": f"Bearer {other.token}"}, 404),
        (path.replace(claim["lease_id"], str(uuid.uuid4())), worker_headers, 404),
    ]:
        assert (await client.get(candidate, headers=headers)).status_code == expected
        response = await client.post(candidate + "/reconcile", headers=headers, json=payload)
        assert response.status_code == expected
    assert await snapshot(worker["id"], claim["lease_id"]) == (6, 12288, 470, 1, "active", 0, 0)


@pytest.mark.parametrize("operation", ["revoke", "quarantine", "shared"])
async def test_recovery_does_not_bypass_revocation_quarantine_or_individual_auth(
    client, worker_headers, assigned_lease, operation,
):
    worker, claim, path, payload = assigned_lease
    await set_work_status(claim, WorkStatus.FAILED)
    headers = worker_headers
    if operation == "shared":
        app.dependency_overrides[get_settings] = lambda: Settings(
            worker_auth_mode="development",
            worker_shared_secret="synthetic-shared-secret-32-characters",
        )
        headers = {"Authorization": "Bearer synthetic-shared-secret-32-characters"}
    else:
        async for session in app.dependency_overrides[get_session]():
            if operation == "revoke":
                token = worker_headers["Authorization"].removeprefix("Bearer kwc_")
                await revoke_credential(
                    session, token.split(".")[0], actor="test", reason="recovery test",
                )
            else:
                await quarantine_worker(session, worker["id"], actor="test", reason="recovery test")
            await session.commit()
    before = await snapshot(worker["id"], claim["lease_id"])
    expected = 403 if operation == "shared" else 401
    assert (await client.get(path, headers=headers)).status_code == expected
    response = await client.post(path + "/reconcile", headers=headers, json=payload)
    assert response.status_code == expected
    assert await snapshot(worker["id"], claim["lease_id"]) == before
