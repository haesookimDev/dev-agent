import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_authorization import authorized as authorized
from test_authorization import create_item, database, sign_in

from app.models import AgentEvent, AuditRecord, ConsoleLease, Principal


async def create_console(client):
    work = await create_item(client)
    async with database() as session:
        session.add(ConsoleLease(work_item_id=work["id"], holder_type="agent", holder="agent",
                                  expires_at=datetime.now(UTC) + timedelta(minutes=15)))
        await session.commit()
    return work


async def test_console_audit_retains_both_sides_of_each_ownership_change(authorized):
    work = await create_console(authorized)
    await sign_in(authorized, "user-123")
    url = f"/api/work-items/{work['id']}/console-lease"
    for version, action in enumerate(("acquire", "acquire", "release"), start=1):
        request_id = str(uuid.uuid4())
        response = await authorized.post(url, json={
            "action": action, "expected_version": version, "holder": "forged",
        }, headers={"X-Request-ID": request_id, "X-Forwarded-For": "203.0.113.1"})
        assert response.status_code == 200
        async with database() as session:
            record = await session.scalar(select(AuditRecord).order_by(AuditRecord.id.desc()))
            principal = await session.scalar(select(Principal).where(
                Principal.subject == "user-123",
            ))
            assert record.action == "console.transferred"
            assert record.actor_id == principal.id
            assert record.actor_subject == "user-123"
            assert record.identity_provider == "https://identity.example"
            assert record.organization_role == "viewer"
            assert record.repository_role == record.effective_role == "approver"
            assert record.required_role == "operator"
            assert record.organization_id == "acme"
            assert record.repository == work["repository"]
            assert record.work_item_id == record.target_id == work["id"]
            assert record.request_id == request_id == response.headers["X-Request-ID"]
            assert record.correlation_id == work["correlation_id"]
            assert record.source_ip == "127.0.0.1"
            assert record.transport == "web"
            expected_before = "agent" if version == 1 else "user"
            expected_after = "agent" if action == "release" else "user"
            assert record.details == {
                "action": action, "holder_type_before": expected_before,
                "holder_before": "agent" if version == 1 else "user-123",
                "version_before": version, "holder_type_after": expected_after,
                "holder_after": "agent" if action == "release" else "user-123",
                "version_after": version + 1,
                "expires_at": datetime.fromisoformat(response.json()["expires_at"]).isoformat(),
            }
    async with database() as session:
        records = list(await session.scalars(select(AuditRecord).order_by(AuditRecord.id)))
        assert len(records) == 3
        assert records[0].details["holder_type_after"] == "user"
        assert records[-1].details["holder_type_after"] == "agent"


@pytest.mark.parametrize("subject,organization,action,version,expected", [
    ("viewer", "acme", "acquire", 1, 403),
    ("admin", "other", "acquire", 1, 404),
    ("operator", "acme", "acquire", 99, 409),
    ("operator", "acme", "release", 1, 409),
])
async def test_rejected_console_change_does_not_create_a_success_audit(
    authorized, subject, organization, action, version, expected,
):
    work = await create_console(authorized)
    await sign_in(authorized, subject, organization)
    response = await authorized.post(f"/api/work-items/{work['id']}/console-lease", json={
        "action": action, "expected_version": version,
    })
    assert response.status_code == expected
    async with database() as session:
        assert not list(await session.scalars(select(AuditRecord)))
        lease = await session.get(ConsoleLease, work["id"])
        assert lease.holder_type == "agent"
        assert lease.version == 1


async def test_other_console_owner_cannot_mutate_or_add_a_success_audit(authorized):
    work = await create_console(authorized)
    url = f"/api/work-items/{work['id']}/console-lease"
    assert (await authorized.post(url, json={"action": "acquire"})).status_code == 200
    await sign_in(authorized, "operator")
    for action in ("acquire", "release"):
        assert (await authorized.post(url, json={"action": action})).status_code == 409
    async with database() as session:
        assert len(list(await session.scalars(select(AuditRecord)))) == 1


async def test_console_audit_failure_rolls_back_ownership_version_expiry_and_event(authorized):
    work = await create_console(authorized)
    async with database() as session:
        lease = await session.get(ConsoleLease, work["id"])
        before_expiry = lease.expires_at
        before_events = list(await session.scalars(select(AgentEvent.id)))
        await session.execute(text(
            "CREATE TRIGGER console_test_audit_failure BEFORE INSERT ON audit_records "
            "BEGIN SELECT RAISE(ABORT, 'test audit unavailable'); END"
        ))
        await session.commit()
    with pytest.raises(IntegrityError, match="test audit unavailable"):
        await authorized.post(f"/api/work-items/{work['id']}/console-lease", json={
            "action": "acquire", "expected_version": 1,
        })
    async with database() as session:
        lease = await session.get(ConsoleLease, work["id"])
        assert lease.holder_type == lease.holder == "agent"
        assert lease.version == 1
        assert lease.expires_at == before_expiry
        assert list(await session.scalars(select(AgentEvent.id))) == before_events
        assert not list(await session.scalars(select(AuditRecord)))
