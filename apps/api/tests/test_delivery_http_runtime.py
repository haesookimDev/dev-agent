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


def test_real_api_rejects_corrupt_backup_then_delivers_only_restored_approved_bytes(tmp_path):
    with delivery_runtime(tmp_path, replace_bundle_on_token=True) as runtime:
        client = runtime.client
        url = f"/api/work-items/{runtime.success_id}"
        runtime.patch_path.write_bytes(b"x" * len(runtime.patch))
        assert client.get(f"{url}/delivery-bundle").status_code == 410
        rejected = client.post(f"{url}/approvals", json={
            "kind": "pull_request", "decision": "approve",
        })
        assert rejected.status_code == 409
        assert client.get(url).json()["status"] == "awaiting_approval"
        assert client.get(f"{url}/audit-log").json() == []
        assert runtime.token_requests == runtime.writes == []
        # Restore the exact retained object, without changing metadata or forging approval.
        runtime.patch_path.write_bytes(runtime.patch)
        assert client.get(f"{url}/delivery-bundle").content == runtime.patch
        approved = client.post(f"{url}/approvals", json={
            "kind": "pull_request", "decision": "approve",
        })
        assert approved.status_code == 200
        deadline = time.monotonic() + 10
        while client.get(url).json()["status"] != "completed":
            assert time.monotonic() < deadline, "verified snapshot was not delivered"
            time.sleep(.05)
        assert b"Tampered delivery" in runtime.patch_path.read_bytes()
        assert runtime.token_requests == [1] and len(runtime.writes) == 1
        assert runtime.git("show", f"{runtime.writes[0]}:README.md",
                           cwd=runtime.bare) == "Approved delivery\n"
        assert client.get(f"{url}/delivery-bundle").status_code == 410
        assert client.get(f"{url}/audit-log").json()[-1]["action"] == "delivery.completed"
        assert not list((tmp_path / "artifacts").glob("delivery-*"))
