import errno
import json
import sys

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import StatusCode
from sqlalchemy import select
from test_delivery_quarantine import pending_delivery as pending_delivery
from test_observability import MemoryExporter
from test_worker_credentials import database as database

from app import delivery
from app.models import AgentEvent, DeliveryJob, WorkItem, WorkStatus

real_command = delivery.run_command


@pytest.mark.parametrize("kind,code", [
    ("runtime", "internal_error"), ("http", "upstream_error"),
    ("timeout", "timeout"), ("http_timeout", "timeout"),
    ("command", "command_failed"), ("path", "filesystem_error"),
])
async def test_failure_evidence_never_retains_private_exception_details(
    pending_delivery, monkeypatch, kind, code,
):
    job = pending_delivery
    marker = "synthetic-private-delivery-value"
    exporter = MemoryExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(delivery, "tracer", provider.get_tracer("delivery-disclosure-test"))

    async def fail(*_, **__):
        if kind == "command":
            await real_command(sys.executable, "-c", f"print({marker!r}); raise SystemExit(7)")
        elif kind == "http":
            request = httpx.Request("GET", f"https://scm.example/private?token={marker}")
            raise httpx.HTTPStatusError(marker, request=request,
                                         response=httpx.Response(503, request=request))
        elif kind == "timeout":
            raise TimeoutError(marker)
        elif kind == "http_timeout":
            raise httpx.ReadTimeout(marker)
        elif kind == "path":
            raise FileNotFoundError(errno.ENOENT, marker, f"/private/{marker}")
        else:
            # Include a private chained cause, not just the top-level message.
            try:
                raise ValueError(marker)
            except ValueError as cause:
                raise RuntimeError(marker) from cause

    job.command.side_effect = fail
    try:
        await delivery.deliver_work(job.work.id)
        async with job.sessions() as session:
            state = await session.get(DeliveryJob, job.work.id)
            assert state.state == "failed"
            assert (await session.get(WorkItem, job.work.id)).status == WorkStatus.FAILED
            events = list((await session.scalars(select(AgentEvent))).all())
            failure = [event for event in events if event.event_type == "delivery.failed"]
            assert len(failure) == 1
            evidence = json.dumps({"job": state.error, "events": [
                {"message": event.message, "payload": event.payload} for event in events
            ], "traces": [span.to_json() for span in exporter.spans]})
            # Only a boolean fails, so even regression diagnostics do not print a credential.
            contains_private_value = marker in evidence
            assert not contains_private_value
            assert failure[0].payload == {"stage": "clone", "error_code": code}
            assert state.error == failure[0].message
            assert "clone" in state.error
        span = exporter.spans[-1]
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes["kelpie.delivery.stage"] == "clone"
        assert span.attributes["kelpie.delivery.error_code"] == code
        assert len(span.events) == 1
        assert span.events[0].name == "exception"
        job.github.create_pull_request.assert_not_awaited()
    finally:
        provider.shutdown()


@pytest.mark.parametrize("stage,method", [
    ("token", "installation_token"), ("metadata", "repository"),
    ("existing_pull_request", "find_pull_request"), ("existing_branch", "branch_exists"),
    ("pull_request", "create_pull_request"),
])
async def test_upstream_failure_preserves_only_a_bounded_stage(pending_delivery, stage, method):
    job = pending_delivery
    getattr(job.github, method).side_effect = httpx.ConnectError("synthetic-private-upstream")
    await delivery.deliver_work(job.work.id)
    async with job.sessions() as session:
        state = await session.get(DeliveryJob, job.work.id)
        event = await session.scalar(select(AgentEvent).where(
            AgentEvent.event_type == "delivery.failed",
        ))
        assert state.state == "failed"
        assert state.error == f"GitHub delivery failed at {stage} (upstream_error)"
        assert event.message == state.error
        assert event.payload == {"stage": stage, "error_code": "upstream_error"}
