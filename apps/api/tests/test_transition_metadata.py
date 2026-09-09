import copy

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_worker_credentials import database as database
from test_worker_quarantine import assigned_work

from app.db import get_session
from app.main import app
from app.models import AgentEvent, WorkItem, WorkSource, WorkStatus
from app.service import transition_work_item
from app.state_machine import ALLOWED_TRANSITIONS


async def create_item(session, initial=WorkStatus.PROVISIONING):
    item = WorkItem(title="Owned transition metadata check", requirement="Check event integrity",
                    repository="acceptance/metadata", source=WorkSource.WEB, status=initial)
    session.add(item)
    await session.flush()
    return item


@pytest.mark.parametrize("initial,target", [
    (initial, target)
    for initial, targets in ALLOWED_TRANSITIONS.items()
    for target in sorted(targets, key=lambda value: value.value)
])
async def test_every_transition_retains_authoritative_state_metadata(database, initial, target):
    item = await create_item(database, initial)
    version = item.version
    payload = {"from": "forged.previous", "to": {"forged": "next"},
               "safe": [17, True, None], "nested": {"from": "source", "to": "destination"}}
    original = copy.deepcopy(payload)
    result = await transition_work_item(database, item, target, expected_version=version,
                                        actor="synthetic:control-plane", payload=payload)
    await database.commit()
    event = await database.scalar(select(AgentEvent).where(AgentEvent.work_item_id == item.id))
    assert (result.status, result.version) == (target, version + 1)
    assert event.payload == {**original, "from": initial.value, "to": target.value}
    assert event.message == f"{initial.value} → {target.value}"
    assert event.source == "synthetic:control-plane"
    assert event.event_type == "work.transitioned"
    assert event.correlation_id == item.correlation_id
    assert payload == original


@pytest.mark.parametrize("payload", [None, {}, {"safe": "kept"}, {"from": None}, {"to": 17}])
async def test_optional_payload_keeps_context_and_explicit_message(database, payload):
    item = await create_item(database)
    original = copy.deepcopy(payload)
    await transition_work_item(
        database, item, WorkStatus.ANALYZING, expected_version=item.version,
        actor="synthetic:worker", message="Explicit context", payload=payload,
    )
    event = await database.scalar(select(AgentEvent))
    assert event.payload == {**(original or {}), "from": "provisioning", "to": "analyzing"}
    assert event.message == "Explicit context"
    assert payload == original


@pytest.mark.parametrize("reason", ["stale-version", "forbidden-transition", "terminal-state"])
async def test_rejected_transition_does_not_mutate_state_or_add_event(database, reason):
    initial = WorkStatus.COMPLETED if reason == "terminal-state" else WorkStatus.PROVISIONING
    item = await create_item(database, initial)
    before = (item.status, item.version, item.updated_at)
    target = WorkStatus.COMPLETED if reason == "forbidden-transition" else WorkStatus.ANALYZING
    with pytest.raises(HTTPException) as error:
        await transition_work_item(database, item, target,
            expected_version=item.version - (reason == "stale-version"), actor="synthetic:worker",
            payload={"from": "queued", "to": "completed"})
    assert error.value.status_code == 409
    assert (item.status, item.version, item.updated_at) == before
    assert await database.scalar(select(AgentEvent)) is None


class _PrivateHeaders(dict):
    def __repr__(self):
        return "<owned test credentials withheld>"


@pytest.fixture
def metadata_headers(worker_headers):
    return _PrivateHeaders(worker_headers)


@pytest.mark.parametrize("payload", [
    {"from": "awaiting_approval", "to": "completed"},
    {"from": None, "to": ["completed"]},
    {"from": "provisioning"}, {"to": "analyzing"}, {},
])
async def test_worker_http_transition_cannot_overwrite_recorded_states(
    client, metadata_headers, payload,
):
    worker, work, headers = await assigned_work(client, metadata_headers)
    original = copy.deepcopy(payload)
    response = await client.post(f"/api/runs/{work['id']}/transition", headers=headers, json={
        "status": "analyzing", "expected_version": 2, "payload": payload,
    })
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["version"]) == ("analyzing", 3)
    events = (await client.get(f"/api/work-items/{work['id']}/event-log")).json()
    event = events[-1]
    assert event["payload"] == {**original, "from": "provisioning", "to": "analyzing"}
    assert event["message"] == "provisioning → analyzing"
    assert event["source"] == f"worker:{worker['id']}"
    assert event["correlation_id"] == work["correlation_id"]
    assert payload == original
    async for session in app.dependency_overrides[get_session]():
        stored = await session.get(AgentEvent, event["id"])
        assert stored.payload == event["payload"]
