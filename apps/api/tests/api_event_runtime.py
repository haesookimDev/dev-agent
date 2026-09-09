"""Direct worker HTTP telemetry drill; deliberately bypass every Runner sanitizer."""

import json
import secrets
import sqlite3


def assert_no_credentials(content: bytes, credentials: list[str]) -> None:
    contains_credential = any(value.encode() in content for value in credentials)
    assert not contains_credential, "credential retained (values withheld)"


def exercise_api_events(runtime, credentials: list[str]) -> int:
    own, foreign = runtime.clients
    work = runtime.works[0]
    headers = runtime.leases[work]
    lease = headers["X-Kelpie-Lease"]
    canary = secrets.token_urlsafe(32)
    credentials.extend([lease, canary, *runtime.tokens])
    before = own.get(f"/api/work-items/{work}").json()
    response = own.post(f"/api/runs/{work}/events", headers=headers, json={
        "event_type": f"worker.probe.{lease}", "source": f"worker:{lease}",
        "message": f"직접 전송 / Direct HTTP: {lease}", "level": "warning",
        "payload": {"headers": {"Authorization": canary},
                    "nested": [{"refreshToken": canary, "safe": "kept"}],
                    "output": lease, f"key-{lease}": [lease, 17], "token_usage": 42},
    })
    assert response.status_code == 200
    assert_no_credentials(response.content, credentials)
    event = response.json()
    assert event["event_type"] == "worker.probe.[REDACTED]"
    assert event["source"] == "worker:[REDACTED]"
    assert event["message"] == "직접 전송 / Direct HTTP: [REDACTED]"
    assert event["payload"] == {
        "headers": {"Authorization": "[REDACTED]"},
        "nested": [{"refreshToken": "[REDACTED]", "safe": "kept"}],
        "output": "[REDACTED]", "key-[REDACTED]": ["[REDACTED]", 17], "token_usage": 42,
    }
    response = own.post(f"/api/runs/{work}/transition", headers=headers, json={
        "status": "analyzing", "expected_version": before["version"],
        "message": f"분석 시작 / Analysis started: {lease}",
        "payload": {"private_key": canary, "output": [lease]},
    })
    assert response.status_code == 200
    assert_no_credentials(response.content, credentials)
    after = response.json()
    assert after["status"] == "analyzing" and after["version"] == before["version"] + 1
    history_url = f"/api/work-items/{work}/event-log"
    events = own.get(history_url).json()
    transition = events[-1]
    assert transition["payload"] == {"from": "provisioning", "to": "analyzing",
                                     "private_key": "[REDACTED]", "output": ["[REDACTED]"]}
    assert transition["message"] == "분석 시작 / Analysis started: [REDACTED]"

    # Subscribe before the next raw HTTP write; verify live delivery, not only replay.
    with own.stream("GET", f"/api/work-items/{work}/events?after={transition['id']}") as stream:
        assert stream.status_code == 200
        response = own.post(f"/api/runs/{work}/events", headers=headers, json={
            "event_type": "worker.live-check", "message": f"실시간 / Live HTTP: {lease}",
            "payload": {"client_secret": canary},
        })
        assert response.status_code == 200
        assert_no_credentials(response.content, credentials)
        sent = response.json()
        for line in stream.iter_lines():
            if line.startswith("data: "):
                assert_no_credentials(line.encode(), credentials)
                received = json.loads(line[6:])
                assert received["id"] == sent["id"]
                assert received["message"] == "실시간 / Live HTTP: [REDACTED]"
                assert received["payload"] == {"client_secret": "[REDACTED]"}
                break
        else:
            raise AssertionError("new direct HTTP event missing from actual SSE")

    events = own.get(history_url).json()
    stable = own.get(f"/api/work-items/{work}").json()
    assert all(stable[key] == after[key] for key in ("status", "version", "correlation_id"))
    for endpoint in ("events", "transition"):
        for invalid_headers in ({}, {"X-Kelpie-Lease": secrets.token_urlsafe(32)},
                                runtime.leases[runtime.works[1]]):
            body = {"message": lease, "payload": {"token": canary}}
            if endpoint == "events":
                body["event_type"] = "worker.denied"
            else:
                body.update(status="implementing", expected_version=after["version"])
            denied = own.post(f"/api/runs/{work}/{endpoint}", headers=invalid_headers, json=body)
            assert denied.status_code == 401
            assert_no_credentials(denied.content, credentials)
    assert own.get(f"/api/work-items/{work}").json() == stable
    retained = own.get(history_url)
    assert retained.status_code == 200 and retained.json() == events
    assert_no_credentials(retained.content, credentials)
    assert all(event["correlation_id"] == before["correlation_id"] for event in events)
    assert foreign.get(history_url).status_code == 404
    with foreign.stream("GET", f"/api/work-items/{work}/events") as denied:
        assert denied.status_code == 404
    with sqlite3.connect(runtime.database) as connection:
        rows = connection.execute(
            "SELECT event_type, source, message, payload FROM agent_events WHERE work_item_id = ?",
            (work,),
        ).fetchall()
    assert_no_credentials(json.dumps(rows).encode(), credentials)
    return len(events)
