import time

from delivery_runtime import delivery_runtime


def test_real_approval_delivery_git_and_audit_http_contract(tmp_path):
    with delivery_runtime(tmp_path) as runtime:
        client = runtime.client
        for work_id, status, outcome in [
            (runtime.success_id, "completed", "delivery.completed"),
            (runtime.failure_id, "failed", "delivery.failed"),
        ]:
            url = f"/api/work-items/{work_id}"
            response = client.post(f"{url}/approvals", json={
                "kind": "pull_request", "decision": "approve",
            })
            assert response.status_code == 200
            assert response.json()["status"] == "committing"
            deadline = time.monotonic() + 10
            while client.get(url).json()["status"] != status:
                assert time.monotonic() < deadline, "delivery did not reach expected state"
                time.sleep(.05)
            evidence = client.get(f"{url}/audit-log")
            assert evidence.status_code == 200
            assert evidence.headers["Cache-Control"] == "no-store"
            source, started, ended = evidence.json()
            assert [row["action"] for row in (source, started, ended)] == [
                "approval.decided", "delivery.started", outcome,
            ]
            assert source["actor_subject"] == "acceptance-admin"
            assert source["request_id"] == response.headers["X-Request-ID"]
            assert started["request_id"] == ended["request_id"] != source["request_id"]
            assert ended["details"]["approval_audit_id"] == source["id"]
            assert ended["transport"] == "background"
            assert ended["actor_id"] is ended["required_role"] is ended["source_ip"] is None
            assert ended["details"]["approved_work_version"] == 11
            assert ended["details"]["approved_bundle_sha256"] == (
                source["details"]["delivery_bundle_sha256"]
            )
            if status == "failed":
                assert ended["details"]["error_code"] == "upstream_error"
                assert ended["details"]["stage"] == "metadata"
            else:
                assert ended["details"]["pull_request_number"] == 42
                assert ended["details"]["publication"] == "new_branch"
            assert "synthetic-private-upstream" not in evidence.text
            duplicate = client.post(f"{url}/approvals", json={
                "kind": "pull_request", "decision": "approve",
            })
            assert duplicate.status_code == 409
            assert client.get(f"{url}/audit-log").json() == evidence.json()
        assert len(runtime.writes) == 1
        count = runtime.git("rev-list", "--count", runtime.writes[0], cwd=runtime.bare).strip()
        assert count == "2"
        assert not list((tmp_path / "artifacts").glob("delivery-*"))
