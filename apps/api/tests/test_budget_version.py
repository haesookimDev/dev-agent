import pytest
from test_authorization import authorized as authorized
from test_authorization import database, sign_in
from test_budget_approval import exhausted_work
from test_worker_transition_authority import retained

from app.models import WorkItem


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("version", [
    {}, {"expected_version": None}, {"expected_version": True}, {"expected_version": False},
    {"expected_version": "1"}, {"expected_version": 1.0}, {"expected_version": 0},
    {"expected_version": -1}, {"expected_version": []}, {"expected_version": {}},
])
async def test_budget_decision_requires_explicit_positive_integer_version(
    authorized, decision, version,
):
    work = await exhausted_work(authorized)
    before = await retained()
    response = await authorized.post(f"/api/work-items/{work['id']}/approvals", json={
        "kind": "budget", "decision": decision, "payload": {"minutes": 15}, **version,
    })
    assert response.status_code == 422
    assert await retained() == before


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("expected_version", [1, 4])
async def test_old_or_future_budget_version_cannot_mutate_same_exhausted_state(
    authorized, decision, expected_version,
):
    work = await exhausted_work(authorized)
    async with database() as session:
        (await session.get(WorkItem, work["id"])).version = 3
        await session.commit()
    before = await retained()
    response = await authorized.post(f"/api/work-items/{work['id']}/approvals", json={
        "kind": "budget", "decision": decision, "expected_version": expected_version,
        "payload": {"minutes": 15},
    })
    assert response.status_code == 409
    assert response.json() == {"detail": "version mismatch: current version is 3"}
    assert await retained() == before


@pytest.mark.parametrize("subject,organization,status", [
    ("viewer", "acme", 403), ("operator", "acme", 403), ("admin", "other", 404),
])
@pytest.mark.parametrize("version", [{}, {"expected_version": 99}])
async def test_budget_version_requirement_does_not_disclose_unauthorized_work(
    authorized, subject, organization, status, version,
):
    work = await exhausted_work(authorized)
    await sign_in(authorized, subject, organization)
    before = await retained()
    response = await authorized.post(f"/api/work-items/{work['id']}/approvals", json={
        "kind": "budget", "decision": "approve", **version,
    })
    assert response.status_code == status
    assert await retained() == before
