"""Reusable private-log and actual connection-state assertions for HTTP/browser drills."""

import time


def assert_stream_log_clean(log):
    failures = ("Exception terminating connection", "non-checked-in connection", "Traceback",
                "Task exception was never retrieved", '"level":"error"')
    if any(failure in log for failure in failures):
        raise AssertionError("Owned stream runtime emitted a connection lifecycle error")


def wait_state(client, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get("/__test/stream-state")
        assert response.status_code == 200
        state = response.json()
        if predicate(state):
            return state
        time.sleep(0.01)
    raise AssertionError("Owned stream runtime did not reach the required connection state")
