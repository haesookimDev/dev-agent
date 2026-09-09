import copy
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from kelpie_runner.main import redact as runner_redact
from sqlalchemy import func, select
from test_api import create_work
from test_worker_quarantine import assigned_work

from app.db import get_session
from app.event_redaction import redact_worker_telemetry
from app.main import app
from app.models import AgentEvent, ResourceLease, WorkerHost, utcnow


class _PrivateHeaders(dict):
    def __repr__(self):
        return "<owned test credentials withheld>"


@pytest.fixture
def redaction_headers(worker_headers):
    return _PrivateHeaders(worker_headers)


def assert_no_credentials(content, *credentials):
    contains_credential = any(value in content for value in credentials)
    assert not contains_credential, "credential retained (values withheld)"


@pytest.mark.parametrize("field", [
    "token", "access_token", "refreshToken", "ID-TOKEN", "session_token",
    "Authorization", "Proxy-Authorization", "Cookie", "Set-Cookie", "API_Key",
    "secret", "client_secret", "password", "passwd", "private_key", "lease_token",
    "X-Kelpie-Lease", "KELPIE_LEASE_TOKEN",
])
def test_api_and_runner_share_the_same_redaction_contract(field):
    lease = secrets.token_urlsafe(32)
    payload = {"nested": [{field: "synthetic-field-value", "safe": (17, True, None)}],
               "output": f"Before {lease} after", f"key-{lease}": lease,
               "token_usage": 42, "secret_count": 2}
    original = copy.deepcopy(payload)
    expected = {"nested": [{field: "[REDACTED]", "safe": [17, True, None]}],
                "output": "Before [REDACTED] after", "key-[REDACTED]": "[REDACTED]",
                "token_usage": 42, "secret_count": 2}
    clean = redact_worker_telemetry(payload, lease=lease)
    assert clean == runner_redact(payload, lease=lease) == expected
    assert redact_worker_telemetry(clean, lease=lease) == clean
    assert payload == original


def test_missing_lease_does_not_rewrite_safe_text_or_skip_explicit_fields():
    assert redact_worker_telemetry({"output": "Safe text", "api_key": "synthetic-value"},
                                   lease=None) == {"output": "Safe text", "api_key": "[REDACTED]"}


async def test_direct_event_ingress_redacts_before_response_and_storage(client, redaction_headers):
    _, work, headers = await assigned_work(client, redaction_headers)
    token = headers["X-Kelpie-Lease"]
    canary = secrets.token_urlsafe(32)
    body = {"event_type": f"probe.{token}", "source": f"worker:{token}", "level": "warning",
            "message": f"Result {token}", "payload": {
                "headers": {"Authorization": canary}, "nested": [{"refreshToken": canary}],
                "output": f"Text {token}", f"key-{token}": [token, 17], "token_usage": 42,
            }}
    original = copy.deepcopy(body)
    response = await client.post(f"/api/runs/{work['id']}/events", headers=headers, json=body)
    assert response.status_code == 200
    assert_no_credentials(response.text, token, canary)
    event = response.json()
    assert event["event_type"] == "probe.[REDACTED]"
    assert event["source"] == "worker:[REDACTED]"
    assert event["message"] == "Result [REDACTED]"
    assert event["level"] == "warning"
    assert event["payload"] == {
        "headers": {"Authorization": "[REDACTED]"}, "nested": [{"refreshToken": "[REDACTED]"}],
        "output": "Text [REDACTED]", "key-[REDACTED]": ["[REDACTED]", 17], "token_usage": 42,
    }
    assert body == original
    history = await client.get(f"/api/work-items/{work['id']}/event-log")
    assert history.status_code == 200
    assert_no_credentials(history.text, token, canary)
    retained = history.json()[-1]
    # SQLite omits the timezone suffix on a later read of the same UTC value.
    saved_time = datetime.fromisoformat(retained["created_at"])
    if saved_time.tzinfo is None:
        saved_time = saved_time.replace(tzinfo=UTC)
    assert saved_time == datetime.fromisoformat(event["created_at"])
    retained["created_at"] = event["created_at"]
    assert retained == event
    async for session in app.dependency_overrides[get_session]():
        stored = await session.get(AgentEvent, event["id"])
        assert stored.message == event["message"]
        assert stored.payload == event["payload"]


async def test_transition_redacts_telemetry_without_changing_state_contract(
    client, redaction_headers,
):
    _, work, headers = await assigned_work(client, redaction_headers)
    token = headers["X-Kelpie-Lease"]
    canary = secrets.token_urlsafe(32)
    before = (await client.get(f"/api/work-items/{work['id']}")).json()
    response = await client.post(f"/api/runs/{work['id']}/transition", headers=headers, json={
        "status": "analyzing", "expected_version": before["version"],
        "message": f"Connected {token}",
        "payload": {"private_key": canary, "output": [token], "safe": "kept"},
    })
    assert response.status_code == 200
    assert response.json()["status"] == "analyzing"
    assert response.json()["version"] == before["version"] + 1
    history = await client.get(f"/api/work-items/{work['id']}/event-log")
    assert_no_credentials(history.text, token, canary)
    event = history.json()[-1]
    assert event["message"] == "Connected [REDACTED]"
    assert event["payload"] == {"from": "provisioning", "to": "analyzing",
        "private_key": "[REDACTED]", "output": ["[REDACTED]"], "safe": "kept"}
    assert event["correlation_id"] == before["correlation_id"]


@pytest.mark.parametrize("endpoint", ["events", "transition"])
@pytest.mark.parametrize("reason", ["missing", "invalid", "other-work", "expired", "quarantined"])
async def test_redaction_does_not_allow_invalid_lease_writes(
    client, redaction_headers, endpoint, reason,
):
    worker, work, headers = await assigned_work(client, redaction_headers)
    token = headers["X-Kelpie-Lease"]
    target = work["id"]
    supplied = dict(headers)
    if reason == "missing":
        supplied = {}
    elif reason == "invalid":
        supplied = {"X-Kelpie-Lease": secrets.token_urlsafe(32)}
    elif reason == "other-work":
        target = (await create_work(client, title="Other owned synthetic work"))["id"]
    async for session in app.dependency_overrides[get_session]():
        if reason == "expired":
            lease = await session.scalar(select(ResourceLease).where(
                ResourceLease.work_item_id == work["id"],
            ))
            lease.expires_at = utcnow() - timedelta(seconds=1)
        elif reason == "quarantined":
            host = await session.get(WorkerHost, worker["id"])
            host.quarantined_at = utcnow()
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(AgentEvent))
    before = (await client.get(f"/api/work-items/{target}")).json()
    body = {"message": f"Must not be retained {token}", "payload": {"token": token}}
    if endpoint == "events":
        body["event_type"] = "rejected.probe"
    else:
        body.update(status="analyzing", expected_version=before["version"])
    response = await client.post(f"/api/runs/{target}/{endpoint}", headers=supplied, json=body)
    assert response.status_code == 401
    assert_no_credentials(response.text, token)
    assert (await client.get(f"/api/work-items/{target}")).json() == before
    async for session in app.dependency_overrides[get_session]():
        assert await session.scalar(select(func.count()).select_from(AgentEvent)) == count


@pytest.mark.parametrize("change", ["stale-version", "forbidden-transition"])
async def test_redaction_preserves_transition_conflict_guards(client, redaction_headers, change):
    _, work, headers = await assigned_work(client, redaction_headers)
    before = (await client.get(f"/api/work-items/{work['id']}")).json()
    history = (await client.get(f"/api/work-items/{work['id']}/event-log")).json()
    response = await client.post(f"/api/runs/{work['id']}/transition", headers=headers, json={
        "status": "analyzing" if change == "stale-version" else "completed",
        "expected_version": before["version"] - (change == "stale-version"),
        "message": headers["X-Kelpie-Lease"], "payload": {"access_token": "synthetic-field"},
    })
    assert response.status_code == 409
    assert_no_credentials(response.text, headers["X-Kelpie-Lease"])
    assert (await client.get(f"/api/work-items/{work['id']}")).json() == before
    assert (await client.get(f"/api/work-items/{work['id']}/event-log")).json() == history
