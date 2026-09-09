import uuid

import pytest
from sqlalchemy import select
from test_authorization import authorized as authorized
from test_authorization import database, sign_in
from test_worker_quarantine import assigned_work

from app.models import (
    AgentEvent,
    Approval,
    AuditRecord,
    DeliveryJob,
    ResourceLease,
    Role,
    WorkerHost,
    WorkItem,
    WorkStatus,
)
from app.state_machine import ALLOWED_TRANSITIONS


class PrivateHeaders(dict):
    def __repr__(self):
        return "<owned credentials withheld>"


@pytest.fixture
def private_worker_headers(worker_headers):
    return PrivateHeaders(worker_headers)


async def prepared(client, private_worker_headers, initial):
    worker, work, headers = await assigned_work(client, private_worker_headers)
    async with database() as session:
        item = await session.get(WorkItem, work["id"])
        item.status = initial
        await session.commit()
    return worker, work["id"], PrivateHeaders(headers)


async def retained():
    async with database() as session:
        return {
            model.__tablename__: [
                {column.name: getattr(row, column.name) for column in model.__table__.columns}
                for row in await session.scalars(select(model))
            ] for model in (WorkItem, ResourceLease, AgentEvent, Approval, AuditRecord, DeliveryJob)
        }


@pytest.mark.parametrize("initial,target", [
    (initial, target) for initial, targets in ALLOWED_TRANSITIONS.items()
    for target in sorted(targets, key=lambda value: value.value)
])
async def test_worker_cannot_take_control_plane_edges_without_authority(
    client, private_worker_headers, initial, target,
):
    _, work, headers = await prepared(client, private_worker_headers, initial)
    before = await retained()
    response = await client.post(f"/api/runs/{work}/transition", headers=headers, json={
        "status": target.value, "expected_version": 2,
        "message": "Claiming approval is not authority",
        "payload": {"approved": True, "actor": "administrator", "delivery_queued": False},
    })
    reserved = (
        initial in {WorkStatus.QUEUED, WorkStatus.FAILED}
        or target in {WorkStatus.COMMITTING, WorkStatus.PR_CREATED, WorkStatus.COMPLETED}
        or (target == WorkStatus.IMPLEMENTING and initial not in {
            WorkStatus.ANALYZING, WorkStatus.VERIFYING,
        })
    )
    assert response.status_code == (403 if reserved else 200)
    if reserved:
        assert await retained() == before  # Includes rolling back lease renewal.
    else:
        assert (response.json()["status"], response.json()["version"]) == (target.value, 3)
        event = (await client.get(f"/api/work-items/{work}/event-log")).json()[-1]
        assert event["payload"]["from"] == initial.value
        assert event["payload"]["to"] == target.value


@pytest.mark.parametrize("case,expected", [
    ("missing", 401), ("invalid", 401), ("stale", 409), ("invalid-edge", 409),
])
async def test_lease_and_conflict_errors_still_precede_authority_checks(
    client, private_worker_headers, case, expected,
):
    _, work, headers = await prepared(client, private_worker_headers, WorkStatus.AWAITING_APPROVAL)
    before = await retained()
    response = await client.post(f"/api/runs/{work}/transition",
        headers={} if case == "missing" else {"X-Kelpie-Lease": "invalid"}
        if case == "invalid" else headers,
        json={"status": "completed" if case == "invalid-edge" else "committing",
              "expected_version": 1 if case == "stale" else 2})
    assert response.status_code == expected
    assert await retained() == before


@pytest.mark.parametrize("initial,action,body", [
    (WorkStatus.AWAITING_APPROVAL, "approvals", {"kind": "pull_request", "decision": "approve"}),
    (WorkStatus.BUDGET_EXHAUSTED, "approvals", {"kind": "budget", "decision": "approve"}),
    (WorkStatus.AWAITING_FEEDBACK, "feedback", {"message": "Resume through the operator"}),
    (WorkStatus.AWAITING_INPUT, "feedback", {"message": "Requested input supplied"}),
    (WorkStatus.AWAITING_APPROVAL, "feedback", {"message": "Revise before approval"}),
])
async def test_real_oidc_user_action_still_authorizes_resume_or_mock_delivery(
    authorized, private_worker_headers, initial, action, body,
):
    _, work, headers = await prepared(authorized, private_worker_headers, initial)
    await sign_in(authorized, "viewer")
    assert (await authorized.post(f"/api/work-items/{work}/{action}", json=body)).status_code == 403
    await sign_in(authorized, "approver" if action == "approvals" else "operator")
    response = await authorized.post(f"/api/work-items/{work}/{action}", json=body)
    assert response.status_code == 200
    if body.get("kind") == "pull_request":
        assert response.json()["status"] == "committing"
        for target in ("pr_created", "completed"):
            response = await authorized.post(f"/api/runs/{work}/transition", headers=headers,
                json={"status": target, "expected_version": response.json()["version"]})
            assert response.status_code == 200
        assert response.json()["status"] == "completed"
    else:
        assert response.json()["status"] == "implementing"
    async with database() as session:
        records = list(await session.scalars(select(AuditRecord)))
        assert len(records) == 1
        assert records[0].actor_subject == ("approver" if action == "approvals" else "operator")


@pytest.mark.parametrize("target", ["pr_created", "completed"])
@pytest.mark.parametrize("change", [
    "no-audit", "real-worker", "delivery-job", "organization", "repository", "correlation",
    "role", "required-role", "decision", "queued", "digest", "version", "version-bool", "state",
])
async def test_mock_completion_rejects_missing_or_mismatched_approval(
    client, private_worker_headers, target, change,
):
    initial = WorkStatus.COMMITTING if target == "pr_created" else WorkStatus.PR_CREATED
    worker, work, headers = await prepared(client, private_worker_headers, initial)
    async with database() as session:
        item = await session.get(WorkItem, work)
        details = {"kind": "pull_request", "decision": "approve", "delivery_queued": False,
                   "delivery_bundle_sha256": None, "work_status_before": "awaiting_approval",
                   "work_status_after": "committing",
                   "work_version_after": item.version - int(target == "completed")}
        values = dict(organization_id=item.organization_id, work_item_id=work,
            repository=item.repository, action="approval.decided", target_id="synthetic-approval",
            actor_subject="synthetic-approver", identity_provider="https://identity.example",
            organization_role=Role.APPROVER,
            effective_role=Role.APPROVER, required_role=Role.APPROVER,
            request_id=str(uuid.uuid4()), correlation_id=item.correlation_id, transport="web")
        if change == "real-worker":
            (await session.get(WorkerHost, worker["id"])).labels = {"virtualization": "libvirt"}
        elif change == "delivery-job":
            session.add(DeliveryJob(work_item_id=work, state="pending"))
        elif change in {"organization", "repository", "correlation"}:
            values[{"organization": "organization_id", "repository": "repository",
                    "correlation": "correlation_id"}[change]] = "different-synthetic-scope"
        elif change in {"role", "required-role"}:
            values["effective_role" if change == "role" else "required_role"] = Role.VIEWER
        elif change in {"decision", "queued", "digest", "version", "version-bool", "state"}:
            key, value = {
                "decision": ("decision", "reject"), "queued": ("delivery_queued", True),
                "digest": ("delivery_bundle_sha256", "a" * 64),
                "version": ("work_version_after", 100),
                "version-bool": ("work_version_after", True),
                "state": ("work_status_after", "completed"),
            }[change]
            details[key] = value
        if change != "no-audit":
            session.add(AuditRecord(**values, details=details))
        await session.commit()
    before = await retained()
    response = await client.post(f"/api/runs/{work}/transition", headers=headers,
        json={"status": target, "expected_version": 2})
    assert response.status_code == 403
    assert await retained() == before
