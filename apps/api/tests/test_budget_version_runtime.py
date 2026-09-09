import json

from api_event_runtime import assert_no_credentials
from artifact_runtime import artifact_runtime
from worker_authority_runtime import PrivateCredentials, admin_client, advance, snapshot


def test_actual_http_rejects_replayed_budget_confirmation_after_another_exhaustion(tmp_path):
    credentials = PrivateCredentials()
    with artifact_runtime(tmp_path, verify_log=lambda log:
                          assert_no_credentials(log.encode(), credentials)) as runtime:
        credentials.extend([*runtime.tokens,
                            *(headers["X-Kelpie-Lease"] for headers in runtime.leases.values())])
        own, foreign = runtime.clients
        url = f"/api/work-items/{runtime.works[0]}"
        advance(runtime, 0, "analyzing")
        first = advance(runtime, 0, "budget_exhausted")
        body = {"kind": "budget", "decision": "approve", "payload": {"minutes": 15},
                "expected_version": first["version"]}
        with admin_client(runtime, 0, credentials) as admin:
            before = snapshot(runtime)
            assert own.post(f"{url}/approvals", json=body).status_code == 403
            assert foreign.post(f"{url}/approvals", json=body).status_code == 404
            missing = {key: value for key, value in body.items() if key != "expected_version"}
            assert admin.post(f"{url}/approvals", json=missing).status_code == 422
            assert admin.post(f"{url}/approvals", json=body,
                headers={"Origin": "https://different.example.invalid"}).status_code == 403
            assert snapshot(runtime) == before
            approved = admin.post(f"{url}/approvals", json=body)
            assert approved.status_code == 200
            assert approved.json()["budget_minutes"] == first["budget_minutes"] + 15
            advance(runtime, 0, "verifying")
            second = advance(runtime, 0, "budget_exhausted")
            assert second["version"] == first["version"] + 3
            before = snapshot(runtime)
            for decision in ("approve", "reject"):
                replay = admin.post(f"{url}/approvals", json=body | {"decision": decision})
                assert replay.status_code == 409
                assert snapshot(runtime) == before
            current = body | {"expected_version": second["version"]}
            rejected = admin.post(f"{url}/approvals", json=current | {"decision": "reject"})
            assert rejected.status_code == 200
            assert rejected.json()["version"] == second["version"]
            assert rejected.json()["budget_minutes"] == second["budget_minutes"]
            final = admin.post(f"{url}/approvals", json=current)
            assert final.status_code == 200 and final.json()["status"] == "implementing"
            assert final.json()["budget_minutes"] == first["budget_minutes"] + 30
            assert final.json()["version"] == second["version"] + 1
            retained = snapshot(runtime)
            assert admin.post(f"{url}/approvals", json=current).status_code == 409
            assert snapshot(runtime) == retained
            audit = admin.get(f"{url}/audit-log")
            assert audit.status_code == 200
            assert [row["details"]["decision"] for row in audit.json()] == [
                "approve", "reject", "approve",
            ]
            assert [row["details"]["work_version_before"] for row in audit.json()] == [
                first["version"], second["version"], second["version"],
            ]
            assert all(row["actor_subject"] == "admin-0" for row in audit.json())
        events = own.get(f"{url}/event-log")
        assert events.status_code == 200
        assert_no_credentials(json.dumps(events.json()).encode(), credentials)
        database = runtime.database
    assert_no_credentials(database.read_bytes(), credentials)
