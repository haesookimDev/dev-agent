"""Runner exits must not strand actively executing work or override user decisions."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from kelpie_runner import main


@pytest.mark.parametrize("state", ["provisioning", "analyzing", "implementing", "verifying"])
@pytest.mark.parametrize("event_failure", [False, True])
def test_execution_failure_reads_current_version_and_marks_failed(
    monkeypatch, tmp_path, state, event_failure,
):
    requests = exercise_failure(monkeypatch, tmp_path, [state], event_failure=event_failure)
    transitions = [json.loads(r.content) for r in requests if r.url.path.endswith("/transition")]
    assert len(transitions) == 1
    assert transitions[0]["status"] == "failed"
    assert transitions[0]["expected_version"] == 7
    assert transitions[0]["message"] == "Runner execution failed"


@pytest.mark.parametrize(
    "state",
    [
        "cancelled",
        "completed",
        "failed",
        "awaiting_approval",
        "awaiting_input",
        "awaiting_feedback",
        "budget_exhausted",
        "committing",
        "pr_created",
        "unknown_future",
    ],
)
def test_failure_preserves_terminal_approval_and_delivery_states(monkeypatch, tmp_path, state):
    requests = exercise_failure(monkeypatch, tmp_path, [state])
    assert not any(r.url.path.endswith("/transition") for r in requests)


def test_failure_rereads_conflict_and_preserves_concurrent_cancellation(monkeypatch, tmp_path):
    requests = exercise_failure(monkeypatch, tmp_path, ["analyzing", "cancelled"], conflict=True)
    assert len([r for r in requests if r.method == "GET"]) == 2
    assert len([r for r in requests if r.url.path.endswith("/transition")]) == 1


def test_unavailable_api_preserves_original_failure_and_closes_client(monkeypatch, tmp_path):
    requests = exercise_failure(monkeypatch, tmp_path, ["analyzing"], read_failure=True)
    assert not any(r.url.path.endswith("/transition") for r in requests)


@pytest.mark.parametrize(
    "work",
    [
        {"id": "different-work", "status": "analyzing", "version": 7},
        {"status": "analyzing", "version": 7},
        {"id": "owned-work", "status": "analyzing", "version": True},
        {"id": "owned-work", "status": "analyzing", "version": "7"},
        {"id": "owned-work", "status": "analyzing", "version": 0},
    ],
)
def test_failure_state_rejects_untrusted_identity_and_version(work):
    async def scenario():
        requests = []

        def respond(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=work)
            return httpx.Response(200, json={})

        control = main.ControlClient(
            "http://runner.invalid", "owned-work", "synthetic-lease", "trace",
        )
        await control.close()
        control.client = httpx.AsyncClient(
            base_url="http://runner.invalid", transport=httpx.MockTransport(respond),
        )
        try:
            await control.fail_execution()
        finally:
            await control.close()
        assert [(request.method, request.url.path) for request in requests] == [
            ("GET", "/api/runs/owned-work"),
        ]

    asyncio.run(scenario())


def exercise_failure(
    monkeypatch, tmp_path, states, *, event_failure=False, conflict=False, read_failure=False,
):
    async def scenario():
        control = main.ControlClient(
            "http://runner.invalid", "owned-work", "synthetic-lease", "trace",
        )
        await control.close()
        requests, current = [], iter(states)

        def respond(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(
                    503 if read_failure else 200,
                    json={"id": "owned-work", "status": next(current), "version": 7},
                )
            if request.url.path.endswith("/events"):
                return httpx.Response(500 if event_failure else 200, json={})
            assert request.url.path.endswith("/transition")
            return httpx.Response(409 if conflict else 200, json={"status": "failed", "version": 8})

        control.client = httpx.AsyncClient(
            base_url="http://runner.invalid", transport=httpx.MockTransport(respond),
        )
        assignment = main.Assignment(
            "owned-work", "trace", "Fixture", "Fail safely", "example/fixture", 2, 60, 0,
        )
        monkeypatch.setenv("KELPIE_CONTROL_URL", "http://runner.invalid")
        monkeypatch.setenv("KELPIE_LEASE_TOKEN", "synthetic-lease")
        monkeypatch.setenv("KELPIE_WORK_ROOT", str(tmp_path))
        monkeypatch.delenv("KELPIE_CONTROL_BOOTSTRAP", raising=False)
        monkeypatch.setattr(main.Assignment, "from_environment", lambda: assignment)
        monkeypatch.setattr(main, "ControlClient", lambda *args: control)
        original = RuntimeError("owned clone failure")
        monkeypatch.setattr(main, "clone_repository", AsyncMock(side_effect=original))
        with pytest.raises(RuntimeError) as failure:
            await main.run()
        assert failure.value is original
        assert control.client.is_closed
        return requests

    return asyncio.run(scenario())
