import pytest
from sqlalchemy import select
from test_authorization import authorized as authorized
from test_authorization import create_item, database, sign_in

from app.models import AgentEvent, Approval, AuditRecord, WorkItem, WorkStatus


async def exhausted_work(client):
    item = await create_item(client)
    async with database() as session:
        work = await session.get(WorkItem, item["id"])
        work.status = WorkStatus.BUDGET_EXHAUSTED
        await session.commit()
    return item


@pytest.mark.parametrize("minutes", [
    None, "invalid", "60", "", True, False, 15.9, 60.0, [], {}, 0, 14, 1441, -1,
])
async def test_invalid_budget_extension_is_rejected_without_mutation_and_can_be_retried(
    authorized, minutes,
):
    item = await exhausted_work(authorized)
    url = f"/api/work-items/{item['id']}/approvals"
    async with database() as session:
        events = list(await session.scalars(select(AgentEvent.id)))
    response = await authorized.post(url, json={
        "kind": "budget", "decision": "approve", "payload": {"minutes": minutes},
    })
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid budget extension"}
    async with database() as session:
        work = await session.get(WorkItem, item["id"])
        assert work.status == WorkStatus.BUDGET_EXHAUSTED
        assert work.budget_minutes == item["budget_minutes"]
        assert work.version == item["version"]
        assert list(await session.scalars(select(AgentEvent.id))) == events
        assert not list(await session.scalars(select(Approval)))
        assert not list(await session.scalars(select(AuditRecord)))
    retried = await authorized.post(url, json={
        "kind": "budget", "decision": "approve", "payload": {"minutes": 45},
    })
    assert retried.status_code == 200
    assert retried.json()["budget_minutes"] == item["budget_minutes"] + 45
    assert retried.json()["status"] == "implementing"
    async with database() as session:
        assert len(list(await session.scalars(select(Approval)))) == 1
        audit = (await session.scalars(select(AuditRecord))).one()
        assert audit.details["budget_minutes_after"] == item["budget_minutes"] + 45


@pytest.mark.parametrize("payload,extension", [({}, 60), ({"minutes": 15}, 15),
                                               ({"minutes": 60}, 60), ({"minutes": 1440}, 1440)])
async def test_integer_budget_bounds_and_omitted_default_are_preserved(
    authorized, payload, extension,
):
    item = await exhausted_work(authorized)
    response = await authorized.post(f"/api/work-items/{item['id']}/approvals", json={
        "kind": "budget", "decision": "approve", "payload": payload,
    })
    assert response.status_code == 200
    assert response.json()["budget_minutes"] == item["budget_minutes"] + extension
    assert response.json()["status"] == "implementing"
    assert response.json()["version"] == item["version"] + 1
    async with database() as session:
        approval = (await session.scalars(select(Approval))).one()
        assert approval.payload == payload
        audit = (await session.scalars(select(AuditRecord))).one()
        assert audit.details["budget_minutes_before"] == item["budget_minutes"]
        assert audit.details["budget_minutes_after"] == item["budget_minutes"] + extension


async def test_rejecting_extension_does_not_require_or_apply_minutes(authorized):
    item = await exhausted_work(authorized)
    response = await authorized.post(f"/api/work-items/{item['id']}/approvals", json={
        "kind": "budget", "decision": "reject", "payload": {"minutes": None},
    })
    assert response.status_code == 200
    assert response.json()["budget_minutes"] == item["budget_minutes"]
    assert response.json()["status"] == "budget_exhausted"
    assert response.json()["version"] == item["version"]
    async with database() as session:
        audit = (await session.scalars(select(AuditRecord))).one()
        assert audit.details["decision"] == "reject"
        assert audit.details["budget_minutes_before"] == audit.details["budget_minutes_after"]


@pytest.mark.parametrize("subject,organization,expected", [
    ("viewer", "acme", 403), ("operator", "acme", 403), ("admin", "other", 404),
])
async def test_invalid_minutes_do_not_bypass_authorization(
    authorized, subject, organization, expected,
):
    item = await exhausted_work(authorized)
    await sign_in(authorized, subject, organization)
    response = await authorized.post(f"/api/work-items/{item['id']}/approvals", json={
        "kind": "budget", "decision": "approve", "payload": {"minutes": None},
    })
    assert response.status_code == expected
    async with database() as session:
        assert not list(await session.scalars(select(Approval)))
        assert not list(await session.scalars(select(AuditRecord)))


async def test_budget_extension_still_requires_exhausted_state(authorized):
    item = await create_item(authorized)
    response = await authorized.post(f"/api/work-items/{item['id']}/approvals", json={
        "kind": "budget", "decision": "approve", "payload": {"minutes": 45},
    })
    assert response.status_code == 409
    async with database() as session:
        assert not list(await session.scalars(select(Approval)))
        assert not list(await session.scalars(select(AuditRecord)))
