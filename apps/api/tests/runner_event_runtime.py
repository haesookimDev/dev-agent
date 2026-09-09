"""Actual Runner HTTP and child-process drill; no Codex account or VM is used."""

import asyncio
import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from kelpie_runner.main import ControlClient, run_verification


def assert_no_credentials(content: bytes, credentials: list[str]) -> None:
    contains_credential = any(value.encode() in content for value in credentials)
    assert not contains_credential, "credential retained (values withheld)"


def exercise_runner_events(runtime, directory: Path, credentials: list[str]) -> int:
    own, foreign = runtime.clients
    work = runtime.works[0]
    current = own.get(f"/api/work-items/{work}").json()
    lease = runtime.leases[work]["X-Kelpie-Lease"]
    field_canary = secrets.token_urlsafe(32)
    credentials.extend([lease, field_canary, *runtime.tokens])

    async def exercise(repository):
        control = ControlClient(runtime.api_url, work, lease, current["correlation_id"])
        try:
            state = await control.transition("analyzing", current["version"],
                                             f"Runner connected: {lease}")
            state = await control.transition("implementing", state["version"],
                                             "Synthetic implementation ready")
            await control.transition("verifying", state["version"],
                                     "Checking actual verification subprocess output")
            await control.event("codex.item.completed", f"Runner event lease: {lease}",
                payload={"headers": {"Authorization": field_canary},
                         "nested": [{"refresh_token": field_canary, "safe": "kept"}],
                         "output": f"Result {lease}", "token_usage": 17})
            for code in (1, 0):
                (repository / ".kelpie.yaml").write_text(json.dumps({"verification": {
                    "commands": [[sys.executable, "probe.py", str(code)]]}}))
                passed, output = await run_verification(repository, control)
                assert passed is (code == 0)
                assert_no_credentials(output.encode(), credentials)
                if code:
                    assert "[REDACTED]" in output
                    assert output.endswith(" visible\n")
                else:
                    assert output == ""
            await control.event("runner.failed", f"Synthetic recoverable failure: {lease}",
                                level="error", payload={"client_secret": field_canary})
        finally:
            await control.close()
        denied = ControlClient(runtime.api_url, work, secrets.token_urlsafe(32),
                               current["correlation_id"])
        try:
            try:
                await denied.event("runner.denied", "This must not be stored")
            except httpx.HTTPStatusError as error:
                assert error.response.status_code == 401
            else:
                raise AssertionError("invalid lease was accepted")
        finally:
            await denied.close()

    with TemporaryDirectory(prefix="runner-probe-", dir=directory) as temporary:
        repository = Path(temporary)
        # Only this owned test lease is read by the child; no environment is dumped.
        descriptor = os.open(repository / "lease", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(lease)
        (repository / "probe.py").write_text(
            "from pathlib import Path\nimport sys\n"
            "print(Path('lease').read_text() + 'x' * 19980 + ' visible')\n"
            "raise SystemExit(int(sys.argv[1]))\n"
        )
        asyncio.run(exercise(repository))

    response = own.get(f"/api/work-items/{work}/event-log")
    assert response.status_code == 200
    assert_no_credentials(response.content, credentials)
    events = response.json()
    selected = [event for event in events if event["event_type"] == "codex.item.completed"]
    assert len(selected) == 1
    assert selected[0]["message"] == "Runner event lease: [REDACTED]"
    assert selected[0]["payload"] == {
        "headers": {"Authorization": "[REDACTED]"},
        "nested": [{"refresh_token": "[REDACTED]", "safe": "kept"}],
        "output": "Result [REDACTED]", "token_usage": 17,
    }
    verification = [event for event in events if event["event_type"] == "verification.completed"]
    assert [event["payload"]["exit_code"] for event in verification] == [1, 0]
    for event in verification:
        assert event["payload"]["output"] == "[REDACTED]" + "x" * 19980 + " visible\n"
    assert not any(event["event_type"] == "runner.denied" for event in events)
    assert all(event["correlation_id"] == current["correlation_id"] for event in events)
    assert foreign.get(f"/api/work-items/{work}/event-log").status_code == 404

    last = events[-1]
    with own.stream("GET", f"/api/work-items/{work}/events?after={last['id'] - 1}") as stream:
        assert stream.status_code == 200
        for line in stream.iter_lines():
            if line.startswith("data: "):
                assert_no_credentials(line.encode(), credentials)
                assert json.loads(line[6:])["message"] == (
                    "Synthetic recoverable failure: [REDACTED]"
                )
                break
        else:
            raise AssertionError("stored Runner event missing from actual SSE")
    with sqlite3.connect(runtime.database) as connection:
        retained = connection.execute(
            "SELECT message, payload FROM agent_events WHERE work_item_id = ?", (work,),
        ).fetchall()
    assert_no_credentials(json.dumps(retained).encode(), credentials)
    return len(events)
