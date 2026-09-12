import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from kelpie_runner import main


def assignment() -> main.Assignment:
    return main.Assignment(
        "owned-work", "trace", "Fixture", "Bootstrap safely", "example/fixture", 2, 60, 0,
    )


async def make_control(respond) -> main.ControlClient:
    control = main.ControlClient(
        "http://runner.invalid", "owned-work", "synthetic-lease", "trace",
    )
    headers = dict(control.client.headers)
    await control.close()
    control.client = httpx.AsyncClient(
        base_url="http://runner.invalid", headers=headers, transport=httpx.MockTransport(respond),
    )
    return control


def test_bootstrap_reads_and_claims_analyzing_before_clone_version():
    async def scenario():
        requests = []

        def respond(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(
                    200, json={"id": "owned-work", "status": "provisioning", "version": 1},
                )
            return httpx.Response(
                200, json={"id": "owned-work", "status": "analyzing", "version": 2},
            )

        control = await make_control(respond)
        try:
            work = await control.bootstrap(assignment())
        finally:
            await control.close()
        assert work == {"id": "owned-work", "status": "analyzing", "version": 2}
        assert [(request.method, request.url.path) for request in requests] == [
            ("GET", "/api/runs/owned-work"),
            ("POST", "/api/runs/owned-work/transition"),
        ]
        assert all(request.headers["X-Kelpie-Lease"] == "synthetic-lease" for request in requests)
        assert json.loads(requests[1].content) == {
            "status": "analyzing",
            "expected_version": 1,
            "message": "Runner control bootstrap completed",
            "payload": {},
        }

    asyncio.run(scenario())


def test_bootstrap_accepts_matching_analyzing_assignment_without_transition():
    async def scenario():
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200, json={"id": "owned-work", "status": "analyzing", "version": 2},
            )

        control = await make_control(respond)
        try:
            work = await control.bootstrap(assignment())
        finally:
            await control.close()
        assert work["version"] == 2
        assert [request.method for request in requests] == ["GET"]

    asyncio.run(scenario())


@pytest.mark.parametrize("after_conflict", ["analyzing", "cancelled"])
def test_bootstrap_conflict_rereads_once_and_only_accepts_matching_analyzing(after_conflict):
    async def scenario():
        requests = []
        reads = iter([
            {"id": "owned-work", "status": "provisioning", "version": 1},
            {"id": "owned-work", "status": after_conflict, "version": 2},
        ])

        def respond(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=next(reads))
            return httpx.Response(409, json={"detail": "version mismatch"})

        control = await make_control(respond)
        try:
            if after_conflict == "analyzing":
                assert (await control.bootstrap(assignment()))["status"] == "analyzing"
            else:
                with pytest.raises(RuntimeError, match="control bootstrap rejected"):
                    await control.bootstrap(assignment())
        finally:
            await control.close()
        assert [request.method for request in requests] == ["GET", "POST", "GET"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "work",
    [
        {"id": "different-work", "status": "analyzing", "version": 2},
        {"id": "owned-work", "status": "analyzing", "version": True},
        {"id": "owned-work", "status": "analyzing", "version": 3},
        {"id": "owned-work", "status": "provisioning", "version": 2},
        {"id": "owned-work", "status": "cancelled", "version": 2},
        {"id": "owned-work", "status": "awaiting_approval", "version": 2},
        {"id": "owned-work", "status": "committing", "version": 2},
        {"id": "owned-work", "status": "completed", "version": 2},
        {"status": "analyzing", "version": 2},
    ],
)
def test_bootstrap_strictly_rejects_wrong_identity_version_or_state(work):
    async def scenario():
        control = await make_control(lambda request: httpx.Response(200, json=work))
        try:
            with pytest.raises(RuntimeError, match="control bootstrap rejected"):
                await control.bootstrap(assignment())
        finally:
            await control.close()

    asyncio.run(scenario())


def test_opt_in_run_bootstraps_before_clone_and_preserves_original_clone_error(
    monkeypatch, tmp_path,
):
    async def scenario():
        requests = []
        read_count = 0

        def respond(request):
            nonlocal read_count
            requests.append(request)
            if request.method == "GET":
                read_count += 1
                return httpx.Response(200, json={
                    "id": "owned-work",
                    "status": "provisioning" if read_count == 1 else "analyzing",
                    "version": 1 if read_count == 1 else 2,
                })
            if request.url.path.endswith("/events"):
                return httpx.Response(200, json={})
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "owned-work", "status": body["status"],
                "version": body["expected_version"] + 1,
            })

        control = await make_control(respond)
        original = RuntimeError("owned clone failure")

        async def clone(*_args):
            assert [(request.method, request.url.path) for request in requests] == [
                ("GET", "/api/runs/owned-work"),
                ("POST", "/api/runs/owned-work/transition"),
            ]
            raise original

        monkeypatch.setenv("KELPIE_CONTROL_URL", "http://runner.invalid")
        monkeypatch.setenv("KELPIE_LEASE_TOKEN", "synthetic-lease")
        monkeypatch.setenv("KELPIE_CONTROL_BOOTSTRAP", "1")
        monkeypatch.setenv("KELPIE_WORK_ROOT", str(tmp_path))
        monkeypatch.setattr(main.Assignment, "from_environment", assignment)
        monkeypatch.setattr(main, "ControlClient", lambda *_args: control)
        monkeypatch.setattr(main, "clone_repository", clone)

        with pytest.raises(RuntimeError) as failure:
            await main.run()
        assert failure.value is original
        assert control.client.is_closed
        transitions = [
            json.loads(request.content) for request in requests
            if request.url.path.endswith("/transition")
        ]
        assert [transition["status"] for transition in transitions] == ["analyzing", "failed"]

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["cancelled", "awaiting_approval", "committing", "completed"])
def test_bootstrap_state_gate_stops_before_clone_without_overwrite(monkeypatch, tmp_path, status):
    async def scenario():
        requests = []

        def respond(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(
                    200, json={"id": "owned-work", "status": status, "version": 9},
                )
            return httpx.Response(200, json={})

        control = await make_control(respond)
        clone = AsyncMock()
        monkeypatch.setenv("KELPIE_CONTROL_URL", "http://runner.invalid")
        monkeypatch.setenv("KELPIE_LEASE_TOKEN", "synthetic-lease")
        monkeypatch.setenv("KELPIE_CONTROL_BOOTSTRAP", "1")
        monkeypatch.setenv("KELPIE_WORK_ROOT", str(tmp_path))
        monkeypatch.setattr(main.Assignment, "from_environment", assignment)
        monkeypatch.setattr(main, "ControlClient", lambda *_args: control)
        monkeypatch.setattr(main, "clone_repository", clone)

        with pytest.raises(RuntimeError, match="control bootstrap rejected"):
            await main.run()
        clone.assert_not_awaited()
        assert control.client.is_closed
        assert not any(request.url.path.endswith("/transition") for request in requests)

    asyncio.run(scenario())


def test_bootstrap_auth_failure_stops_before_clone_and_preserves_http_error(monkeypatch, tmp_path):
    async def scenario():
        control = await make_control(lambda request: httpx.Response(401))
        clone = AsyncMock()
        monkeypatch.setenv("KELPIE_CONTROL_URL", "http://runner.invalid")
        monkeypatch.setenv("KELPIE_LEASE_TOKEN", "synthetic-lease")
        monkeypatch.setenv("KELPIE_CONTROL_BOOTSTRAP", "1")
        monkeypatch.setenv("KELPIE_WORK_ROOT", str(tmp_path))
        monkeypatch.setattr(main.Assignment, "from_environment", assignment)
        monkeypatch.setattr(main, "ControlClient", lambda *_args: control)
        monkeypatch.setattr(main, "clone_repository", clone)

        with pytest.raises(httpx.HTTPStatusError) as failure:
            await main.run()
        assert failure.value.response.status_code == 401
        clone.assert_not_awaited()
        assert control.client.is_closed

    asyncio.run(scenario())


def test_unknown_nonempty_bootstrap_mode_fails_before_clone(monkeypatch, tmp_path):
    async def scenario():
        control = SimpleNamespace(
            event=AsyncMock(), fail_execution=AsyncMock(), close=AsyncMock(),
        )
        clone = AsyncMock()
        monkeypatch.setenv("KELPIE_CONTROL_URL", "http://runner.invalid")
        monkeypatch.setenv("KELPIE_LEASE_TOKEN", "synthetic-lease")
        monkeypatch.setenv("KELPIE_CONTROL_BOOTSTRAP", "unexpected")
        monkeypatch.setenv("KELPIE_WORK_ROOT", str(tmp_path))
        monkeypatch.setattr(main.Assignment, "from_environment", assignment)
        monkeypatch.setattr(main, "ControlClient", lambda *_args: control)
        monkeypatch.setattr(main, "clone_repository", clone)

        with pytest.raises(RuntimeError, match="unsupported KELPIE_CONTROL_BOOTSTRAP mode"):
            await main.run()
        clone.assert_not_awaited()
        control.fail_execution.assert_awaited_once()
        control.close.assert_awaited_once()

    asyncio.run(scenario())
