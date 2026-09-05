import sqlite3
import time

from runtime_health_runtime import await_observation, runtime_health_runtime

AVAILABLE = ("kelpie_runtime_snapshot_available", ())
QUEUED = ("kelpie_runtime_queued_work", ())
OFFLINE = ("kelpie_runtime_workers", (("state", "offline"),))
EXPIRED = ("kelpie_runtime_leases", (("state", "expired"),))


def test_real_api_query_failure_recovers_after_http_heartbeat_and_cancellation(tmp_path):
    with runtime_health_runtime(tmp_path) as runtime:
        client = runtime.client
        initial = await_observation(client, lambda value: value[AVAILABLE] == 1)
        assert initial[QUEUED] == initial[OFFLINE] == initial[EXPIRED] == 1
        assert initial["kelpie_runtime_queue_oldest_age_seconds", ()] >= 1200
        runtime.query_failure(True)
        failed = await_observation(client, lambda value: value[AVAILABLE] == 0)
        assert failed[QUEUED] == failed[OFFLINE] == failed[EXPIRED] == 1
        # /readyz checks the revision, whereas continuous observations catch this query failure.
        assert client.get("/readyz").status_code == 200
        started = time.monotonic()
        assert client.get("/metrics").status_code == 200
        assert time.monotonic() - started < 0.5
        runtime.query_failure(False)
        runtime.heartbeat()  # Actual scoped Worker credential and HTTP boundary, no real Worker/VM.
        cancelled = client.post(f"/api/work-items/{runtime.queued_id}/cancel",
                                json={"expected_version": 1})
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        recovered = await_observation(client, lambda value: value[AVAILABLE] == 1)
        assert recovered[QUEUED] == recovered[OFFLINE] == 0
        # Observation and queue cancellation did not release an unrelated active lease.
        assert recovered[EXPIRED] == 1
        with sqlite3.connect(runtime.database) as connection:
            states = connection.execute("SELECT state FROM resource_leases").fetchall()
            assert states == [("active",)]
        log = (tmp_path / "api.log").read_text()
        assert "runtime health observation failed; retrying" in log
        for private in ("runtime_private_worker_fixture", "SELECT", "Traceback", "Authorization"):
            assert private not in log
