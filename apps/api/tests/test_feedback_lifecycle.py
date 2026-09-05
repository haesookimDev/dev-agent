import pytest
from sqlalchemy import select
from test_authorization import authorized as authorized
from test_authorization import create_item, database, sign_in
from test_integration_authorization import slack_request

from app.models import AgentEvent, AuditRecord, Feedback, WorkItem, WorkStatus


@pytest.mark.parametrize("channel", ["web", "slack"])
@pytest.mark.parametrize("initial,expected", [
    (WorkStatus.QUEUED, WorkStatus.QUEUED),
    (WorkStatus.PROVISIONING, WorkStatus.PROVISIONING),
    (WorkStatus.ANALYZING, WorkStatus.ANALYZING),
    (WorkStatus.IMPLEMENTING, WorkStatus.IMPLEMENTING),
    (WorkStatus.VERIFYING, WorkStatus.VERIFYING),
    (WorkStatus.AWAITING_FEEDBACK, WorkStatus.IMPLEMENTING),
    (WorkStatus.AWAITING_APPROVAL, WorkStatus.IMPLEMENTING),
    (WorkStatus.AWAITING_INPUT, WorkStatus.IMPLEMENTING),
    (WorkStatus.BUDGET_EXHAUSTED, WorkStatus.BUDGET_EXHAUSTED),
    (WorkStatus.COMMITTING, None),
    (WorkStatus.PR_CREATED, None),
    (WorkStatus.COMPLETED, None),
    (WorkStatus.FAILED, None),
    (WorkStatus.CANCELLED, None),
])
async def test_feedback_respects_work_lifecycle(authorized, channel, initial, expected):
    item = await create_item(authorized)
    async with database() as session:
        work = await session.get(WorkItem, item["id"])
        work.status = initial
        await session.commit()
        await session.refresh(work)
        before_version = work.version
        before_updated = work.updated_at
        before_events = list(await session.scalars(select(AgentEvent.id).order_by(AgentEvent.id)))

    if channel == "slack":
        response = await slack_request(authorized, f"feedback {item['id']} Review empty states")
    else:
        response = await authorized.post(f"/api/work-items/{item['id']}/feedback", json={
            "message": "Review empty states",
        })

    assert response.status_code == (409 if expected is None else 200), response.text
    async with database() as session:
        work = await session.get(WorkItem, item["id"])
        feedback = list(await session.scalars(select(Feedback)))
        audits = list(await session.scalars(select(AuditRecord)))
        events = list(await session.scalars(select(AgentEvent).order_by(AgentEvent.id)))
        if expected is None:
            assert response.json()["detail"] == "work no longer accepts feedback"
            assert not feedback
            assert not audits
            assert [event.id for event in events] == before_events
            assert work.status == initial
            assert work.version == before_version
            assert work.updated_at == before_updated
        else:
            assert len(feedback) == 1
            assert len(audits) == 1
            assert audits[0].target_id == str(feedback[0].id)
            assert audits[0].transport == channel
            assert feedback[0].message == "Review empty states"
            assert feedback[0].channel == channel
            received = [event for event in events if event.event_type == "feedback.received"]
            assert len(received) == 1
            assert received[0].payload == {"channel": channel}
            assert work.status == expected
            assert work.version == before_version + int(initial != expected)


@pytest.mark.parametrize("identity,expected", [
    ({"subject": "viewer"}, 403),
    ({"organization": "other"}, 404),
])
async def test_closed_feedback_does_not_bypass_authorization(authorized, identity, expected):
    item = await create_item(authorized)
    async with database() as session:
        work = await session.get(WorkItem, item["id"])
        work.status = WorkStatus.COMPLETED
        await session.commit()
    await sign_in(authorized, **identity)
    response = await authorized.post(f"/api/work-items/{item['id']}/feedback", json={
        "message": "Late feedback",
    })
    assert response.status_code == expected
