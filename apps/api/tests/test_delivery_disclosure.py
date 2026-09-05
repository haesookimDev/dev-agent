import asyncio
import errno
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import StatusCode
from sqlalchemy import select, text
from test_delivery_quarantine import pending_delivery as pending_delivery
from test_observability import MemoryExporter
from test_worker_credentials import database as database

from app import delivery
from app.models import AgentEvent, AuditRecord, DeliveryBundle, DeliveryJob, WorkItem, WorkStatus

real_command = delivery.run_command


@pytest.mark.parametrize("action", ["delivery.started", "delivery.completed"])
async def test_database_audit_failures_do_not_export_private_errors(
    pending_delivery, monkeypatch, action,
):
    job = pending_delivery
    marker = "synthetic-private-database-value"
    exporter = MemoryExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(delivery, "tracer", provider.get_tracer("delivery-db-disclosure-test"))
    async with job.sessions() as session:
        await session.execute(text(
            "CREATE TRIGGER test_audit_failure BEFORE INSERT ON audit_records "
            f"WHEN NEW.action = '{action}' BEGIN SELECT RAISE(ABORT, '{marker}'); END"
        ))
        await session.commit()
    try:
        with pytest.raises(delivery.DeliveryPersistenceError):
            await delivery.deliver_work(job.work.id)
        assert exporter.spans[-1].status.status_code == StatusCode.ERROR
        contains_private_value = marker in json.dumps([span.to_json() for span in exporter.spans])
        assert not contains_private_value
        assert len(exporter.spans[-1].events) == 1
    finally:
        provider.shutdown()


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
            audits = list(await session.scalars(select(AuditRecord).where(
                AuditRecord.transport == "background",
            ).order_by(AuditRecord.id)))
            assert [record.action for record in audits] == ["delivery.started", "delivery.failed"]
            assert audits[0].request_id == audits[1].request_id
            assert audits[1].details["stage"] == "clone"
            assert audits[1].details["error_code"] == code
            assert audits[1].details["approval_audit_id"] == job.approval.id
            evidence = json.dumps({"job": state.error, "events": [
                {"message": event.message, "payload": event.payload} for event in events
            ], "audits": [record.details for record in audits],
                "traces": [span.to_json() for span in exporter.spans]})
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


async def test_real_git_patch_failure_does_not_publish_a_private_filename(
    pending_delivery, tmp_path,
):
    job = pending_delivery
    source = tmp_path / "source"
    environment = {
        "PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{source.as_uri()}.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/acme/test.git",
    }
    await real_command("git", "init", "--initial-branch=main", str(source),
                       environment=environment)
    await asyncio.to_thread((source / "README.md").write_text, "Local acceptance repository\n")
    await real_command("git", "add", "README.md", cwd=source, environment=environment)
    await real_command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                       "commit", "-m", "Test fixture", cwd=source, environment=environment)
    marker = "synthetic-private-patch-path"
    patch = (f"diff --git a/{marker} b/{marker}\n--- a/{marker}\n+++ b/{marker}\n"
             "@@ -1 +1 @@\n-before\n+after\n").encode()
    # Establish that real Git, not a mocked exception, emits the private filename.
    probe = await asyncio.create_subprocess_exec(
        "git", "apply", "--index", "--binary", "-", cwd=source, env=environment,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await probe.communicate(patch)
    assert probe.returncode != 0 and marker.encode() in stderr
    async with job.sessions() as session:
        bundle = await session.get(DeliveryBundle, job.work.id)
        await asyncio.to_thread(Path(bundle.object_path).write_bytes, patch)

    async def command(*args, **kwargs):
        kwargs["environment"] = {**kwargs.get("environment", {}), **environment}
        return await real_command(*args, **kwargs)

    job.command.side_effect = command
    await delivery.deliver_work(job.work.id)
    async with job.sessions() as session:
        state = await session.get(DeliveryJob, job.work.id)
        events = list((await session.scalars(select(AgentEvent))).all())
        assert state.state == "failed"
        assert state.error == "GitHub delivery failed at apply (command_failed)"
        assert all(marker not in event.message for event in events)
        assert (await session.get(WorkItem, job.work.id)).status == WorkStatus.FAILED
    assert [call.args[:2] for call in job.command.await_args_list] == [
        ("git", "clone"), ("git", "checkout"), ("git", "apply"),
    ]
    job.github.create_pull_request.assert_not_awaited()
    assert not list((tmp_path / "artifacts").glob("delivery-*"))
