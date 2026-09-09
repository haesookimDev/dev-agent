import asyncio
import copy
import json
import secrets
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from kelpie_runner.main import CodexAppServer, ControlClient, redact


@pytest.mark.parametrize("key", [
    "token", "access_token", "refreshToken", "ID-TOKEN", "client_secret",
    "apiKey", "API_KEY", "password", "private_key", "Authorization",
    "Proxy-Authorization", "Cookie", "Set-Cookie", "X-Kelpie-Lease",
    "KELPIE_LEASE_TOKEN",
])
def test_nested_credential_fields_are_redacted_without_changing_input(key):
    payload = {"nested": [{key: "synthetic-field-value", "safe": "kept"}],
               "token_usage": 42, "secret_count": 2, "ok": True, "empty": None}
    original = copy.deepcopy(payload)
    expected = copy.deepcopy(payload)
    expected["nested"][0][key] = "[REDACTED]"
    assert redact(payload) == expected
    assert payload == original


@pytest.mark.parametrize("event_type", ["codex.item.completed", "verification.completed",
                                       "runner.failed"])
def test_every_event_egress_redacts_lease_and_metadata_but_preserves_auth(event_type):
    async def scenario():
        lease = secrets.token_urlsafe(32)
        control = ControlClient("http://runner.invalid", "work-id", lease, "correlation-id")
        headers = dict(control.client.headers)
        await control.client.aclose()
        requests = []

        def capture(request):
            requests.append(request)
            return httpx.Response(200, json={})

        control.client = httpx.AsyncClient(base_url="http://runner.invalid", headers=headers,
                                         transport=httpx.MockTransport(capture))
        payload = {"output": f"before {lease} after", "nested": [{"api_key": "probe"}],
                   f"key-{lease}": [lease, 7], "token_usage": 17}
        original = copy.deepcopy(payload)
        try:
            await control.event(event_type, f"message {lease}", source=f"runner-{lease}",
                                payload=payload)
            await control.transition("verifying", 3, f"transition {lease}")
        finally:
            await control.close()
        assert payload == original
        assert len(requests) == 2
        for request in requests:
            assert request.headers["X-Kelpie-Lease"] == lease
            assert request.headers["X-Kelpie-Correlation-ID"] == "correlation-id"
            if lease.encode() in request.content:
                raise AssertionError("lease escaped in request body (values withheld)")
        event = json.loads(requests[0].content)
        assert event["event_type"] == event_type
        assert event["message"] == "message [REDACTED]"
        assert event["source"] == "runner-[REDACTED]"
        assert event["payload"] == {"output": "before [REDACTED] after",
            "nested": [{"api_key": "[REDACTED]"}], "key-[REDACTED]": ["[REDACTED]", 7],
            "token_usage": 17}
        assert json.loads(requests[1].content) == {"status": "verifying", "expected_version": 3,
            "message": "transition [REDACTED]", "payload": {}}

    asyncio.run(scenario())


def test_redacted_request_still_raises_permission_failure():
    async def scenario():
        control = ControlClient("http://runner.invalid", "work-id", "synthetic-lease", "trace")
        headers = dict(control.client.headers)
        await control.client.aclose()
        control.client = httpx.AsyncClient(base_url="http://runner.invalid", headers=headers,
            transport=httpx.MockTransport(lambda request: httpx.Response(401)))
        try:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await control.event("verification.failed", "synthetic-lease")
            assert error.value.response.status_code == 401
        finally:
            await control.close()

    asyncio.run(scenario())


def test_redaction_is_local_to_client_and_safe_to_repeat():
    async def scenario():
        one = ControlClient("http://runner.invalid", "one", "synthetic-one", "trace")
        two = ControlClient("http://runner.invalid", "two", "synthetic-two", "trace")
        try:
            value = {"output": "synthetic-one synthetic-two", "safe": (1, False, None)}
            clean = one.redact(value)
            assert clean == {"output": "[REDACTED] synthetic-two", "safe": [1, False, None]}
            assert one.redact(clean) == clean
            assert two.redact(value)["output"] == "synthetic-one [REDACTED]"
            assert redact("normal output") == "normal output"
        finally:
            await one.close()
            await two.close()

    asyncio.run(scenario())


def test_codex_message_is_redacted_before_display_limit(monkeypatch):
    async def scenario():
        lease = secrets.token_urlsafe(32)
        control = ControlClient("http://runner.invalid", "work", lease, "trace")
        session = CodexAppServer(Path("."), control)
        session.thread_id = "thread"
        messages = iter([
            {"method": "item/completed", "params": {
                "item": {"text": "x" * 3980 + lease + " visible"},
                "headers": {"authorization": "synthetic-header"}}},
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
        ])
        events = []

        async def read():
            return next(messages)

        async def send(message):
            pass

        def capture(request):
            events.append(json.loads(request.content))
            if lease.encode() in request.content:
                raise AssertionError("lease escaped in Codex event (value withheld)")
            return httpx.Response(200, json={})

        headers = dict(control.client.headers)
        await control.client.aclose()
        control.client = httpx.AsyncClient(base_url="http://runner.invalid", headers=headers,
                                         transport=httpx.MockTransport(capture))
        monkeypatch.setattr(session, "_read", read)
        monkeypatch.setattr(session, "_send", send)
        try:
            await session.run_turn("Synthetic prompt")
        finally:
            await control.close()
        assert len(events) == 2
        assert events[0]["message"] == "x" * 3980 + "[REDACTED] visible"
        assert events[0]["payload"]["headers"] == {"authorization": "[REDACTED]"}

    asyncio.run(scenario())


def test_codex_stderr_is_redacted_before_tail_limit():
    async def scenario():
        lease = secrets.token_urlsafe(32)
        control = ControlClient("http://runner.invalid", "work", lease, "trace")
        session = CodexAppServer(Path("."), control)
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        stdout.feed_eof()
        stderr.feed_data((lease + "x" * 3980).encode())
        stderr.feed_eof()
        session.process = SimpleNamespace(stdout=stdout, stderr=stderr)
        try:
            with pytest.raises(RuntimeError) as error:
                await session._read()
            assert str(error.value) == (
                "Codex App Server exited unexpectedly: [REDACTED]" + "x" * 3980
            )
        finally:
            await control.close()

    asyncio.run(scenario())
