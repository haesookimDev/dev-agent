import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from test_authorization import authorized as authorized
from test_authorization import create_item, database, sign_in

from app.models import (
    AgentEvent,
    AuditRecord,
    Membership,
    Principal,
    RepositoryGrant,
    ResourceLease,
    Role,
    WorkerHost,
    WorkItem,
    WorkStatus,
    utcnow,
)


async def cancel(client, work, **kwargs):
    return await client.post(f"/api/work-items/{work['id']}/cancel",
                             json={"expected_version": work["version"]}, **kwargs)


async def test_cancel_queued_work_records_authenticated_identity_and_state(authorized):
    work = await create_item(authorized)
    request_id = str(uuid.uuid4())
    response = await cancel(authorized, work, headers={
        "X-Request-ID": request_id, "X-Forwarded-For": "203.0.113.9",
        "X-Kelpie-User": "forged", "X-Kelpie-Role": "viewer",
    })
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["version"] == work["version"] + 1
    assert response.json()["assigned_worker_id"] is None
    async with database() as session:
        record = await session.scalar(select(AuditRecord))
        principal = await session.scalar(select(Principal).where(Principal.subject == "admin"))
        assert record.action == "work.cancelled"
        assert record.target_id == record.work_item_id == work["id"]
        assert record.organization_id == "acme"
        assert record.repository == work["repository"]
        assert record.actor_id == principal.id
        assert record.actor_subject == "admin"
        assert record.identity_provider == "https://identity.example"
        assert record.organization_role == record.effective_role == "administrator"
        assert record.repository_role is None
        assert record.required_role == "administrator"
        assert record.request_id == request_id == response.headers["X-Request-ID"]
        assert record.correlation_id == work["correlation_id"]
        assert record.source_ip == "127.0.0.1"
        assert record.transport == "web"
        assert record.details == {
            "scope": "unassigned_queue", "work_status_before": "queued",
            "work_status_after": "cancelled", "work_version_before": 1,
            "work_version_after": 2,
        }
        events = list(await session.scalars(select(AgentEvent).order_by(AgentEvent.id)))
        assert [event.event_type for event in events] == ["work.created", "work.transitioned"]
        assert events[-1].payload == {"from": "queued", "to": "cancelled"}
        assert not list(await session.scalars(select(ResourceLease)))
    audit = await authorized.get(f"/api/work-items/{work['id']}/audit-log")
    assert audit.headers["Cache-Control"] == "no-store"
    assert audit.json()[0]["action"] == "work.cancelled"
    assert (await cancel(authorized, work)).status_code == 409
    assert (await cancel(authorized, response.json())).status_code == 409
    async with database() as session:
        assert len(list(await session.scalars(select(AuditRecord)))) == 1


@pytest.mark.parametrize("subject,organization,expected", [
    ("viewer", "acme", 403), ("operator", "acme", 403), ("approver", "acme", 403),
    ("user-123", "acme", 403), ("admin", "other", 404),
])
async def test_cancel_requires_same_organization_administrator(
    authorized, subject, organization, expected,
):
    work = await create_item(authorized)
    await sign_in(authorized, subject, organization)
    response = await cancel(authorized, work, headers={"X-Kelpie-Role": "administrator"})
    assert response.status_code == expected
    async with database() as session:
        assert (await session.get(WorkItem, work["id"])).status == WorkStatus.QUEUED
        assert not list(await session.scalars(select(AuditRecord)))


async def test_repository_admin_grant_is_scoped_and_revoked_for_existing_session(authorized):
    first = await create_item(authorized)
    second = await create_item(authorized, "acme/second")
    third = await create_item(authorized)
    async with database() as session:
        grant = await session.scalar(select(RepositoryGrant))
        grant.role = Role.ADMINISTRATOR
        await session.commit()
    await sign_in(authorized, "user-123")
    assert (await cancel(authorized, second)).status_code == 403
    assert (await cancel(authorized, first)).status_code == 200
    async with database() as session:
        record = await session.scalar(select(AuditRecord))
        assert record.organization_role == "viewer"
        assert record.repository_role == record.effective_role == "administrator"
        await session.execute(delete(RepositoryGrant).where(
            RepositoryGrant.repository == "acme/service",
        ))
        await session.commit()
    assert (await cancel(authorized, third)).status_code == 403


@pytest.mark.parametrize("denial", ["anonymous", "cross_origin", "revoked_membership"])
async def test_cancel_rechecks_authentication_and_csrf(authorized, denial):
    work = await create_item(authorized)
    if denial == "anonymous":
        authorized.cookies.clear()
    elif denial == "cross_origin":
        authorized.headers["Origin"] = "https://evil.example"
    else:
        async with database() as session:
            await session.execute(delete(Membership))
            await session.commit()
    assert (await cancel(authorized, work)).status_code == (401 if denial == "anonymous" else 403)
    async with database() as session:
        assert not list(await session.scalars(select(AuditRecord)))
        assert (await session.get(WorkItem, work["id"])).version == 1


@pytest.mark.parametrize("payload", [{}, {"expected_version": 0}, {"expected_version": True},
                                      {"expected_version": "1"}, {"expected_version": 1.5}])
async def test_cancel_requires_explicit_positive_integer_version(authorized, payload):
    work = await create_item(authorized)
    response = await authorized.post(f"/api/work-items/{work['id']}/cancel", json=payload)
    assert response.status_code == 422


async def test_stale_cancel_does_not_change_work_or_add_audit(authorized):
    work = await create_item(authorized)
    assert (await cancel(authorized, {**work, "version": 99})).status_code == 409
    async with database() as session:
        assert (await session.get(WorkItem, work["id"])).version == 1
        assert len(list(await session.scalars(select(AgentEvent)))) == 1
        assert not list(await session.scalars(select(AuditRecord)))


@pytest.mark.parametrize("state", [value for value in WorkStatus if value != WorkStatus.QUEUED])
async def test_cancel_does_not_stop_active_or_terminal_work(authorized, state):
    work = await create_item(authorized)
    async with database() as session:
        item = await session.get(WorkItem, work["id"])
        item.status = state
        await session.commit()
    assert (await cancel(authorized, work)).status_code == 409
    async with database() as session:
        assert (await session.get(WorkItem, work["id"])).status == state
        assert not list(await session.scalars(select(AuditRecord)))


@pytest.mark.parametrize("reservation", ["assigned", "active", "expired", "released"])
async def test_inconsistent_queued_work_with_execution_history_cannot_be_cancelled(
    authorized, reservation,
):
    work = await create_item(authorized)
    async with database() as session:
        worker = WorkerHost(name="cancel-safety-test", cpu_total=2, cpu_available=2,
                            memory_mb_total=4096, memory_mb_available=4096,
                            disk_gb_available=30)
        session.add(worker)
        await session.flush()
        if reservation == "assigned":
            (await session.get(WorkItem, work["id"])).assigned_worker_id = worker.id
        else:
            session.add(ResourceLease(work_item_id=work["id"], worker_id=worker.id,
                token_hash="0" * 64, state="active" if reservation == "expired" else reservation,
                expires_at=utcnow() + timedelta(minutes=-1 if reservation == "expired" else 5)))
        await session.commit()
    assert (await cancel(authorized, work)).status_code == 409
    async with database() as session:
        assert (await session.get(WorkItem, work["id"])).status == WorkStatus.QUEUED
        assert not list(await session.scalars(select(AuditRecord)))


async def test_audit_failure_rolls_back_cancellation_and_transition_event(authorized):
    work = await create_item(authorized)
    async with database() as session:
        before_updated_at = (await session.get(WorkItem, work["id"])).updated_at
        await session.execute(text(
            "CREATE TRIGGER cancellation_audit_failure BEFORE INSERT ON audit_records "
            "BEGIN SELECT RAISE(ABORT, 'test audit unavailable'); END"
        ))
        await session.commit()
    with pytest.raises(IntegrityError, match="test audit unavailable"):
        await cancel(authorized, work)
    async with database() as session:
        item = await session.get(WorkItem, work["id"])
        assert item.status == WorkStatus.QUEUED
        assert item.version == 1
        assert item.updated_at == before_updated_at
        assert len(list(await session.scalars(select(AgentEvent)))) == 1
        assert not list(await session.scalars(select(AuditRecord)))
