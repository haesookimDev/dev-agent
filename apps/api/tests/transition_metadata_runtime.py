"""Actual HTTP transitions and operator feedback using disposable scoped identities."""

import json
import sqlite3

from api_event_runtime import assert_no_credentials


def move_with_conflicting_context(runtime, target):
    own = runtime.clients[0]
    work = runtime.works[0]
    headers = runtime.leases[work]
    before = own.get(f"/api/work-items/{work}").json()
    events = own.get(f"/api/work-items/{work}/event-log").json()
    expected = {"from": before["status"], "to": target, "safe": [17, True, None],
                "nested": {"from": "source", "to": "destination"}, "token": "[REDACTED]"}
    with own.stream("GET", f"/api/work-items/{work}/events?after={events[-1]['id']}") as stream:
        assert stream.status_code == 200
        response = own.post(f"/api/runs/{work}/transition", headers=headers, json={
            "status": target, "expected_version": before["version"],
            "payload": {"from": "forged.previous", "to": {"forged": "completed"},
                        "safe": [17, True, None], "nested": expected["nested"],
                        "token": headers["X-Kelpie-Lease"]},
        })
        assert response.status_code == 200
        after = response.json()
        assert (after["status"], after["version"]) == (target, before["version"] + 1)
        for line in stream.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                assert event["event_type"] == "work.transitioned"
                assert event["payload"] == expected
                assert event["message"] == f"{before['status']} → {target}"
                assert event["source"] == f"worker:{after['assigned_worker_id']}"
                assert event["correlation_id"] == before["correlation_id"]
                break
        else:
            raise AssertionError("new transition missing from actual SSE")
    retained = own.get(f"/api/work-items/{work}/event-log")
    assert retained.status_code == 200
    assert_no_credentials(retained.content, [headers["X-Kelpie-Lease"], *runtime.tokens])
    assert retained.json()[-1]["id"] == event["id"]
    assert retained.json()[-1]["payload"] == expected
    with sqlite3.connect(runtime.database) as connection:
        row = connection.execute("SELECT payload FROM agent_events WHERE id = ?",
                                 (event["id"],)).fetchone()
    assert json.loads(row[0]) == expected
    return after


def assert_feedback_resume(runtime):
    own = runtime.clients[0]
    work = runtime.works[0]
    current = own.get(f"/api/work-items/{work}").json()
    assert current["status"] == "implementing"
    events = own.get(f"/api/work-items/{work}/event-log").json()
    feedback, resumed = events[-2:]
    assert feedback["event_type"] == "feedback.received"
    assert resumed["event_type"] == "work.transitioned"
    assert resumed["payload"] == {"from": "awaiting_feedback", "to": "implementing"}
    assert resumed["source"] == feedback["source"] == "operator-0"
    assert resumed["correlation_id"] == current["correlation_id"]
    return current


def exercise_transition_metadata(runtime):
    own, foreign = runtime.clients
    work = runtime.works[0]
    for target in ("analyzing", "implementing", "verifying", "awaiting_feedback"):
        move_with_conflicting_context(runtime, target)
    response = own.post(f"/api/work-items/{work}/feedback", json={
        "message": "Verify authoritative transition metadata", "channel": "web",
    })
    assert response.status_code == 200
    assert_feedback_resume(runtime)
    stable = own.get(f"/api/work-items/{work}").json()
    history_url = f"/api/work-items/{work}/event-log"
    events = own.get(history_url).json()
    body = {"status": "verifying", "expected_version": stable["version"],
            "payload": {"from": "forged.previous", "to": "completed"}}
    for headers, change, expected_status in (
        ({}, {}, 401), (runtime.leases[runtime.works[1]], {}, 401),
        (runtime.leases[work], {"expected_version": stable["version"] - 1}, 409),
        (runtime.leases[work], {"status": "completed"}, 409),
    ):
        response = own.post(f"/api/runs/{work}/transition", headers=headers, json=body | change)
        assert response.status_code == expected_status
    assert own.get(f"/api/work-items/{work}").json() == stable
    assert own.get(history_url).json() == events
    assert foreign.get(history_url).status_code == 404
    with foreign.stream("GET", f"/api/work-items/{work}/events") as denied:
        assert denied.status_code == 404
    return len(events)
