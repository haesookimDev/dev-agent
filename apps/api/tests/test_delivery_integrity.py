import asyncio
from pathlib import Path

import pytest
from delivery_fixtures import PATCH_CONTENT, PATCH_SHA256
from sqlalchemy import select
from test_delivery_quarantine import pending_delivery as pending_delivery
from test_worker_credentials import database as database

from app import delivery
from app.models import AgentEvent, AuditRecord, DeliveryBundle, DeliveryJob, WorkItem, WorkStatus


@pytest.mark.parametrize("change,code", [
    ("missing", "bundle_unavailable"), ("outside", "bundle_unavailable"),
    ("symlink", "bundle_unavailable"), ("corrupt", "bundle_integrity_failed"),
    ("size", "bundle_integrity_failed"),
])
@pytest.mark.parametrize("recover", [False, True])
async def test_invalid_bytes_stop_delivery_before_any_external_action(pending_delivery, change,
                                                                   code, recover):
    job = pending_delivery
    if change == "missing":
        await asyncio.to_thread(job.patch_path.unlink)
    elif change == "corrupt":
        await asyncio.to_thread(job.patch_path.write_bytes, b"x" * len(PATCH_CONTENT))
    elif change == "symlink":
        target = job.patch_path.with_name("private-target.patch")
        await asyncio.to_thread(job.patch_path.rename, target)
        await asyncio.to_thread(job.patch_path.symlink_to, target)
    async with job.sessions() as session:
        bundle = await session.get(DeliveryBundle, job.work.id)
        if change == "outside":
            bundle.object_path = str(job.patch_path.parent.parent.parent / "private.patch")
        elif change == "size":
            bundle.size_bytes += 1
        if recover:
            (await session.get(DeliveryJob, job.work.id)).state = "running"
        await session.commit()
    if recover:
        await delivery.resume_pending_deliveries()
    else:
        await delivery.deliver_work(job.work.id)
    job.github.installation_token.assert_not_awaited()
    job.github.repository.assert_not_awaited()
    job.github.create_pull_request.assert_not_awaited()
    job.command.assert_not_awaited()
    async with job.sessions() as session:
        work = await session.get(WorkItem, job.work.id)
        state = await session.get(DeliveryJob, job.work.id)
        assert work.status == WorkStatus.FAILED and state.state == "failed"
        assert state.error == f"GitHub delivery failed at bundle ({code})"
        records = list(await session.scalars(select(AuditRecord).order_by(AuditRecord.id)))
        failure = records[-1]
        assert failure.action == "delivery.failed"
        assert failure.details["stage"] == "bundle"
        assert failure.details["error_code"] == code
        assert failure.details["authorization"] == "denied"
        assert failure.details["approved_bundle_sha256"] == PATCH_SHA256
        assert failure.details["publication"] == "not_started"
        events = list(await session.scalars(select(AgentEvent)))
        assert any(e.payload == {"stage": "bundle", "error_code": code} for e in events)
        assert "private" not in str([r.details for r in records])
    assert not list(job.patch_path.parent.parent.glob("delivery-*"))


async def test_git_consumes_verified_snapshot_even_if_original_changes_after_verification(
    pending_delivery,
):
    job = pending_delivery
    applied = []

    async def token(*_):
        await asyncio.to_thread(job.patch_path.write_bytes, b"Unapproved content")
        return "synthetic-delivery-token"

    async def command(*args, **_):
        if args[:2] == ("git", "apply"):
            snapshot = Path(args[-1])
            assert snapshot != job.patch_path
            assert await asyncio.to_thread(snapshot.read_bytes) == PATCH_CONTENT
            applied.append(snapshot)
        return ""

    job.github.installation_token.side_effect = token
    job.command.side_effect = command
    await delivery.deliver_work(job.work.id)
    assert len(applied) == 1
    assert not applied[0].exists()
    job.github.create_pull_request.assert_awaited_once()
    async with job.sessions() as session:
        assert (await session.get(WorkItem, job.work.id)).status == WorkStatus.COMPLETED
