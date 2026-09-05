import os
import time

import pytest
from postgres_restore_runtime import restored_api
from postgres_restore_seed import ORGANIZATION

pytestmark = pytest.mark.skipif(
    not (os.environ.get("KELPIE_TEST_POSTGRES_URL")
         and os.environ.get("KELPIE_TEST_POSTGRES_CONTAINER")),
    reason="dedicated PostgreSQL URL and test container not set",
)


def test_restored_api_keeps_identity_scope_and_cannot_replay_delivery(tmp_path):
    with restored_api(tmp_path) as runtime:
        client = runtime.client
        session = client.get("/auth/session")
        assert session.status_code == 200
        assert session.json()["organization"] == ORGANIZATION
        assert session.json()["role"] == "viewer"
        response = client.get("/api/work-items")
        assert response.status_code == 200
        assert [work["id"] for work in response.json()] == [runtime.seed.work_id]
        work = client.get(f"/api/work-items/{runtime.seed.work_id}")
        assert work.status_code == 200 and work.json()["status"] == "committing"
        assert client.get(f"/api/work-items/{runtime.seed.other_id}").status_code == 404
        assert client.get(f"/api/work-items/{runtime.seed.work_id}/audit-log").status_code == 403
        events = client.get(f"/api/work-items/{runtime.seed.work_id}/event-log")
        assert events.status_code == 200 and len(events.json()) == 1
        artifacts = client.get(f"/api/work-items/{runtime.seed.work_id}/artifacts")
        assert artifacts.status_code == 200 and len(artifacts.json()) == 1
        # A DB row is not a restored object: missing bytes must not count as artifact recovery.
        artifact_id = artifacts.json()[0]["id"]
        assert client.get(
            f"/api/work-items/{runtime.seed.work_id}/artifacts/{artifact_id}",
        ).status_code == 410
        client.cookies.clear()
        assert client.get("/api/work-items").status_code == 401
        deadline = time.monotonic() + 3
        while "delivery recovery failed; retrying" not in runtime.log_path.read_text():
            assert time.monotonic() < deadline, "startup recovery did not exercise reader boundary"
            time.sleep(0.05)
        log = runtime.log_path.read_text()
        assert runtime.seed.token not in log
        assert "Traceback" not in log and "SELECT " not in log and "PASSWORD" not in log
        assert client.get("/readyz").status_code == 200
