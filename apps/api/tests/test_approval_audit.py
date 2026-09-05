import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_authorization import authorized as authorized
from test_authorization import create_item, database, sign_in
from test_integration_authorization import slack_request

from app.models import (
    AgentEvent,
    Approval,
    AuditRecord,
    DeliveryBundle,
    DeliveryJob,
    Principal,
    WorkItem,
    WorkStatus,
)


async def prepare_work(client, monkeypatch, kind="pull_request"):
    item = await create_item(client)
    async with database() as session:
        work = await session.get(WorkItem, item["id"])
        work.status = (WorkStatus.BUDGET_EXHAUSTED if kind == "budget"
                       else WorkStatus.AWAITING_APPROVAL)
        work.github_installation_id = 12
        session.add(DeliveryBundle(work_item_id=work.id, sha256="a" * 64,
                                  object_path="test-only.patch", size_bytes=123))
        await session.commit()
        item["status"] = work.status.value
    monkeypatch.setattr("app.main.github", SimpleNamespace(configured=True))
    deliveries = []

    async def delivered(work_id):
        deliveries.append(work_id)

    monkeypatch.setattr("app.main.deliver_work", delivered)
    return item, deliveries


@pytest.mark.parametrize("transport,kind,choice", [
    ("web", "pull_request", "approve"), ("slack", "pull_request", "approve"),
    ("web", "pull_request", "reject"), ("web", "budget", "approve"),
    ("web", "budget", "reject"), ("web", "console", "approve"),
    ("web", "console", "reject"),
])
async def test_approval_audit_captures_identity_roles_and_bounded_before_after_details(
    authorized, monkeypatch, transport, kind, choice,
):
    item, deliveries = await prepare_work(authorized, monkeypatch, kind)
    await sign_in(authorized, "user-123")
    request_id = str(uuid.uuid4())
    authorized.headers.update({"X-Request-ID": request_id, "X-Actor-ID": "forged",
                               "X-Role": "administrator", "X-Forwarded-For": "203.0.113.1"})
    private_text = "Synthetic content that must not be copied into the audit"
    if transport == "slack":
        response = await slack_request(authorized, f"approve {item['id']}")
    else:
        response = await authorized.post(f"/api/work-items/{item['id']}/approvals", json={
            "kind": kind, "decision": choice, "payload": {"minutes": 45, "reason": private_text},
        })
    assert response.status_code == 200
    central = kind == "pull_request" and choice == "approve"
    changed = kind == "pull_request" or (kind == "budget" and choice == "approve")
    final_status = "committing" if central else "implementing" if changed else item["status"]
    async with database() as session:
        record = await session.scalar(select(AuditRecord))
        approval = await session.scalar(select(Approval))
        principal = await session.scalar(select(Principal).where(Principal.subject == "user-123"))
        assert record.action == "approval.decided"
        assert record.target_id == str(approval.id)
        assert record.actor_id == principal.id
        assert record.actor_subject == "user-123"
        assert record.identity_provider == "https://identity.example"
        assert record.organization_role == "viewer"
        assert record.repository_role == record.effective_role == record.required_role == "approver"
        assert record.organization_id == "acme"
        assert record.work_item_id == item["id"]
        assert record.repository == item["repository"]
        assert record.request_id == request_id == response.headers["X-Request-ID"]
        assert record.correlation_id == item["correlation_id"]
        assert record.source_ip == "127.0.0.1"
        assert record.transport == transport
        assert record.details == {
            "kind": kind, "decision": choice,
            "budget_minutes_before": item["budget_minutes"],
            "budget_minutes_after": item["budget_minutes"] + (45 if kind == "budget"
                                                              and choice == "approve" else 0),
            "work_status_before": item["status"], "work_status_after": final_status,
            "work_version_before": item["version"],
            "work_version_after": item["version"] + int(changed),
            "delivery_queued": central, "delivery_bundle_sha256": "a" * 64 if central else None,
        }
        snapshot = {column.name: getattr(record, column.name)
                    for column in record.__table__.columns}
        assert private_text not in str(snapshot)
        assert "forged" not in str(snapshot)
        work = await session.get(WorkItem, item["id"])
        assert work.status.value == final_status
        work.status = WorkStatus.COMPLETED
        work.budget_minutes += 15
        await session.commit()
    async with database() as session:
        record = await session.scalar(select(AuditRecord))
        assert {column.name: getattr(record, column.name)
                for column in record.__table__.columns} == snapshot
    assert deliveries == ([item["id"]] if central else [])


@pytest.mark.parametrize("transport,kind", [
    ("web", "pull_request"), ("slack", "pull_request"), ("web", "budget"),
])
async def test_audit_failure_rolls_back_approval_budget_state_events_and_delivery(
    authorized, monkeypatch, transport, kind,
):
    item, deliveries = await prepare_work(authorized, monkeypatch, kind)
    async with database() as session:
        await session.execute(text(
            "CREATE TRIGGER audit_test_unavailable BEFORE INSERT ON audit_records "
            "BEGIN SELECT RAISE(ABORT, 'test audit storage unavailable'); END"
        ))
        await session.commit()
        events = list(await session.scalars(select(AgentEvent.id)))
    with pytest.raises(IntegrityError, match="test audit storage unavailable"):
        if transport == "slack":
            await slack_request(authorized, f"approve {item['id']}")
        else:
            await authorized.post(f"/api/work-items/{item['id']}/approvals", json={
                "kind": kind, "decision": "approve", "payload": {"minutes": 45},
            })
    async with database() as session:
        for model in (Approval, AuditRecord, DeliveryJob):
            assert not list(await session.scalars(select(model)))
        assert list(await session.scalars(select(AgentEvent.id))) == events
        work = await session.get(WorkItem, item["id"])
        assert work.status.value == item["status"]
        assert work.version == item["version"]
        assert work.budget_minutes == item["budget_minutes"]
    assert deliveries == []


@pytest.mark.parametrize("transport", ["web", "slack"])
async def test_stale_approval_does_not_create_a_second_audit(authorized, monkeypatch, transport):
    item, deliveries = await prepare_work(authorized, monkeypatch)

    async def approve():
        if transport == "slack":
            return await slack_request(authorized, f"approve {item['id']}")
        return await authorized.post(f"/api/work-items/{item['id']}/approvals", json={
            "kind": "pull_request", "decision": "approve",
        })

    assert (await approve()).status_code == 200
    assert (await approve()).status_code == 409
    async with database() as session:
        assert len(list(await session.scalars(select(AuditRecord)))) == 1
        assert len(list(await session.scalars(select(Approval)))) == 1
    assert deliveries == [item["id"]]


@pytest.mark.parametrize("subject,organization,expected", [
    ("viewer", "acme", 403), ("operator", "acme", 403), ("admin", "other", 404),
])
async def test_denied_approval_has_no_success_audit(
    authorized, monkeypatch, subject, organization, expected,
):
    item, deliveries = await prepare_work(authorized, monkeypatch)
    await sign_in(authorized, subject, organization)
    response = await authorized.post(f"/api/work-items/{item['id']}/approvals", json={
        "kind": "pull_request", "decision": "approve",
    })
    assert response.status_code == expected
    async with database() as session:
        assert not list(await session.scalars(select(AuditRecord)))
        assert not list(await session.scalars(select(Approval)))
        assert (await session.get(WorkItem, item["id"])).status.value == item["status"]
    assert deliveries == []


@pytest.mark.parametrize("transport", ["web", "slack"])
async def test_unverified_bundle_cannot_be_approved_or_audited(authorized, transport):
    item = await create_item(authorized)
    async with database() as session:
        (await session.get(WorkItem, item["id"])).status = WorkStatus.AWAITING_APPROVAL
        await session.commit()
    if transport == "slack":
        response = await slack_request(authorized, f"approve {item['id']}")
    else:
        response = await authorized.post(f"/api/work-items/{item['id']}/approvals", json={
            "kind": "pull_request", "decision": "approve",
        })
    assert response.status_code == 409
    async with database() as session:
        assert not list(await session.scalars(select(AuditRecord)))
        assert not list(await session.scalars(select(Approval)))
