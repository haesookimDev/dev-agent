import time

from execution_health_runtime import execution_health_runtime
from runtime_health_runtime import await_observation

AVAILABLE = ("kelpie_runtime_snapshot_available", ())
IMPLEMENTING = ("kelpie_runtime_execution_work", (("state", "implementing"),))
COMMITTING = ("kelpie_runtime_execution_work", (("state", "committing"),))
PENDING = ("kelpie_runtime_delivery_jobs", (("state", "pending"),))
RUNNING = ("kelpie_runtime_delivery_jobs", (("state", "running"),))
COMPLETED = ("kelpie_runtime_delivery_jobs", (("state", "completed"),))


def test_actual_http_execution_observation_survives_delivery_query_failure_and_recovers(tmp_path):
    with execution_health_runtime(tmp_path) as runtime:
        before = runtime.snapshot()
        initial = await_observation(runtime.client, lambda v: v.get(COMMITTING) == 2)
        assert initial[AVAILABLE] == 1
        assert initial[IMPLEMENTING] == initial[PENDING] == initial[RUNNING] == 1
        assert initial["kelpie_runtime_execution_oldest_update_age_seconds",
                       (("state", "implementing"),)] >= 2400
        assert initial["kelpie_runtime_delivery_oldest_update_age_seconds",
                       (("state", "pending"),)] >= 900
        # No lifecycle, resource or audit mutation by observation.
        assert runtime.snapshot() == before
        runtime.query_failure(True)
        failed = await_observation(runtime.client, lambda v: v[AVAILABLE] == 0)
        assert failed[IMPLEMENTING] == failed[PENDING] == failed[RUNNING] == 1
        assert runtime.client.get("/readyz").status_code == 200
        started = time.monotonic()
        assert runtime.client.get("/metrics").status_code == 200
        assert time.monotonic() - started < 0.5
        runtime.query_failure(False)
        runtime.recover_synthetic_states()
        recovered = await_observation(runtime.client, lambda v: v[AVAILABLE] == 1)
        assert recovered[IMPLEMENTING] == recovered[COMMITTING] == 0
        assert recovered[PENDING] == recovered[RUNNING] == 0
        assert recovered[COMPLETED] == 2
        after = runtime.snapshot()
        for table in ("resource_leases", "worker_hosts", "audit_records"):
            assert after[table] == before[table]
        assert recovered["kelpie_runtime_leases", (("state", "expired"),)] == 1
    log = (tmp_path / "api.log").read_text()
    assert "runtime health observation failed; retrying" in log
    for private in ("runtime_private_delivery_fixture", "SELECT", "Traceback",
                    "synthetic private delivery diagnostic", "Authorization"):
        assert private not in log
