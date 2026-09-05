import uuid

import pytest
from fastapi import Request
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from test_authorization import authorized as authorized
from test_authorization import create_item, database, sign_in
from test_integration_authorization import slack_request

from app.audit import request_source_ip
from app.models import (
    AgentEvent,
    AuditRecord,
    Feedback,
    Principal,
    RepositoryGrant,
    WorkItem,
    WorkStatus,
)


@pytest.mark.parametrize("transport", ["web", "slack"])
async def test_feedback_records_authenticated_identity_and_effective_roles(authorized, transport):
    item = await create_item(authorized)
    await sign_in(authorized, "user-123")
    request_id = str(uuid.uuid4())
    authorized.headers.update({
        "X-Request-ID": request_id, "X-Forwarded-For": "203.0.113.99",
        "X-Actor-ID": "forged", "X-Role": "administrator",
    })
    message = "Synthetic sensitive content must not be copied into audit records"
    if transport == "slack":
        response = await slack_request(authorized, f"feedback {item['id']} {message}")
    else:
        response = await authorized.post(f"/api/work-items/{item['id']}/feedback", json={
            "message": message, "channel": "slack",  # Transport must not trust the payload.
        })
    assert response.status_code == 200
    async with database() as session:
        record = await session.scalar(select(AuditRecord))
        principal = await session.scalar(select(Principal).where(
            Principal.issuer == "https://identity.example", Principal.subject == "user-123",
        ))
        feedback = await session.scalar(select(Feedback))
        assert record.actor_id == principal.id
        assert record.actor_subject == "user-123"
        assert record.identity_provider == "https://identity.example"
        assert record.organization_role == "viewer"
        assert record.repository_role == record.effective_role == "approver"
        assert record.required_role == "operator"
        assert record.organization_id == "acme"
        assert record.work_item_id == item["id"]
        assert record.repository == item["repository"]
        assert record.target_id == str(feedback.id)
        assert record.action == "feedback.created"
        assert record.request_id == response.headers["X-Request-ID"] == request_id
        assert record.correlation_id == item["correlation_id"]
        assert record.source_ip == "127.0.0.1"
        assert record.transport == transport
        assert record.created_at is not None
        snapshot = {column.name: getattr(record, column.name)
                    for column in record.__table__.columns}
        assert message not in str(snapshot)
        assert "forged" not in str(snapshot)
        await session.execute(delete(RepositoryGrant).where(
            RepositoryGrant.principal_id == principal.id,
        ))
        await session.commit()
    if transport == "slack":
        denied = await slack_request(authorized, f"feedback {item['id']} denied")
    else:
        denied = await authorized.post(f"/api/work-items/{item['id']}/feedback", json={
            "message": "denied",
        })
    assert denied.status_code == 403
    async with database() as session:
        records = list(await session.scalars(select(AuditRecord)))
        assert len(records) == 1
        assert {column.name: getattr(records[0], column.name)
                for column in records[0].__table__.columns} == snapshot


@pytest.mark.parametrize("transport", ["web", "slack"])
async def test_audit_write_failure_rolls_back_feedback_events_and_status(authorized, transport):
    item = await create_item(authorized)
    async with database() as session:
        work = await session.get(WorkItem, item["id"])
        work.status = WorkStatus.AWAITING_FEEDBACK
        await session.execute(text(
            "CREATE TRIGGER audit_test_unavailable BEFORE INSERT ON audit_records "
            "BEGIN SELECT RAISE(ABORT, 'test audit storage unavailable'); END"
        ))
        await session.commit()
        before = list(await session.scalars(select(AgentEvent.id)))
    with pytest.raises(IntegrityError, match="test audit storage unavailable"):
        if transport == "slack":
            await slack_request(authorized, f"feedback {item['id']} Must roll back")
        else:
            await authorized.post(f"/api/work-items/{item['id']}/feedback", json={
                "message": "Must roll back",
            })
    async with database() as session:
        assert not list(await session.scalars(select(Feedback)))
        assert not list(await session.scalars(select(AuditRecord)))
        assert list(await session.scalars(select(AgentEvent.id))) == before
        work = await session.get(WorkItem, item["id"])
        assert work.status == WorkStatus.AWAITING_FEEDBACK
        assert work.version == item["version"]


async def test_audit_read_is_administrator_only_isolated_paginated_and_not_cached(authorized):
    item = await create_item(authorized)
    other = await create_item(authorized)
    for work in (item, other, item):
        assert (await authorized.post(f"/api/work-items/{work['id']}/feedback", json={
            "message": "Pagination fixture",
        })).status_code == 200
    url = f"/api/work-items/{item['id']}/audit-log"
    first = await authorized.get(url, params={"limit": 1})
    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    for method in ("POST", "PATCH", "DELETE"):
        assert (await authorized.request(method, url)).status_code == 405
    assert len(first.json()) == 1
    next_page = await authorized.get(url, params={"after": first.json()[0]["id"], "limit": 1})
    assert len(next_page.json()) == 1
    assert next_page.json()[0]["id"] > first.json()[0]["id"]
    assert next_page.json()[0]["work_item_id"] == item["id"]
    assert (await authorized.get(url, params={"after": next_page.json()[0]["id"]})).json() == []
    for params in ({"after": -1}, {"limit": 0}, {"limit": 1001}):
        assert (await authorized.get(url, params=params)).status_code == 422
    for subject in ("viewer", "operator", "approver", "user-123"):
        await sign_in(authorized, subject)
        assert (await authorized.get(url)).status_code == 403
    await sign_in(authorized, organization="other")
    assert (await authorized.get(url)).status_code == 404


async def test_repository_administrator_can_read_only_its_audit(authorized):
    first = await create_item(authorized)
    second = await create_item(authorized, "acme/second")
    async with database() as session:
        principal = await session.scalar(select(Principal).where(Principal.subject == "user-123"))
        grant = await session.get(RepositoryGrant, ("acme/service", principal.id))
        grant.role = "administrator"
        await session.commit()
    await sign_in(authorized, "user-123")
    assert (await authorized.get(f"/api/work-items/{first['id']}/audit-log")).json() == []
    assert (await authorized.get(f"/api/work-items/{second['id']}/audit-log")).status_code == 403


@pytest.mark.parametrize("identity,expected", [
    ({"subject": "viewer"}, 403), ({"organization": "other"}, 404),
])
async def test_denied_feedback_does_not_record_a_success(authorized, identity, expected):
    item = await create_item(authorized)
    await sign_in(authorized, **identity)
    response = await authorized.post(f"/api/work-items/{item['id']}/feedback", json={
        "message": "Must be denied",
    })
    assert response.status_code == expected
    async with database() as session:
        assert not list(await session.scalars(select(AuditRecord)))


async def test_development_audit_is_explicit_and_invalid_request_id_is_replaced(client):
    item = await create_item(client)
    response = await client.post(f"/api/work-items/{item['id']}/feedback", json={
        "message": "Development fixture",
    }, headers={"X-Request-ID": "not-an-identifier"})
    assert response.status_code == 200
    async with database() as session:
        record = await session.scalar(select(AuditRecord))
        assert record.actor_id is None
        assert record.identity_provider == "development"
        assert str(uuid.UUID(record.request_id)) == response.headers["X-Request-ID"]


@pytest.mark.parametrize("client,expected", [
    (("2001:db8::1", 1234), "2001:db8::1"), (("testclient", 1234), None), (None, None),
])
def test_source_ip_handles_ipv6_and_missing_transport_metadata(client, expected):
    assert request_source_ip(Request({"type": "http", "client": client})) == expected
