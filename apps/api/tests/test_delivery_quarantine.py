import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_worker_credentials import database as database

from app import delivery
from app.config import Settings
from app.models import AgentEvent, DeliveryBundle, DeliveryJob, WorkItem, WorkSource, WorkStatus
from app.worker_credentials import issue_credential
from app.worker_quarantine import quarantine_worker


@pytest.fixture
async def pending_delivery(database, tmp_path, monkeypatch):
    identity = await issue_credential(database, "delivery-worker", actor="test", reason="test")
    work = WorkItem(source=WorkSource.WEB, title="Test delivery", requirement="Test delivery",
                    repository="acme/test", status=WorkStatus.COMMITTING,
                    assigned_worker_id=identity.worker_id, github_installation_id=1)
    database.add(work)
    await database.flush()
    database.add_all([
        DeliveryJob(work_item_id=work.id),
        DeliveryBundle(work_item_id=work.id, object_path=str(tmp_path / "verified.patch"),
                        sha256="0" * 64, size_bytes=0),
    ])
    await database.commit()
    sessions = async_sessionmaker(database.bind, expire_on_commit=False)
    github = SimpleNamespace(
        installation_token=AsyncMock(return_value="synthetic-delivery-token"),
        repository=AsyncMock(return_value={"default_branch": "main"}),
        find_pull_request=AsyncMock(return_value=None),
        branch_exists=AsyncMock(return_value=False),
        create_pull_request=AsyncMock(return_value="https://github.com/acme/test/pull/1"),
    )
    command = AsyncMock(return_value="")
    monkeypatch.setattr(delivery, "SessionLocal", sessions)
    monkeypatch.setattr(delivery, "github", github)
    monkeypatch.setattr(delivery, "run_command", command)
    monkeypatch.setattr(delivery, "settings", Settings(artifact_root=str(tmp_path / "artifacts")))
    return SimpleNamespace(work=work, worker_id=identity.worker_id, sessions=sessions,
                           github=github, command=command)


async def quarantine(job):
    async with job.sessions() as session:
        await quarantine_worker(session, job.worker_id, actor="test", reason="incident regression")
        await session.commit()


async def assert_quarantined(job):
    async with job.sessions() as session:
        assert (await session.get(WorkItem, job.work.id)).status == WorkStatus.FAILED
        state = await session.get(DeliveryJob, job.work.id)
        assert state.state == "quarantined"
        assert state.error == "worker quarantined; delivery blocked"
        events = (await session.scalars(select(AgentEvent.event_type))).all()
        assert events.count("worker.quarantined") == 1
        assert "delivery.failed" not in events


async def test_pending_quarantined_delivery_is_not_started_or_resumed(pending_delivery):
    job = pending_delivery
    await quarantine(job)
    await delivery.deliver_work(job.work.id)
    await delivery.resume_pending_deliveries()
    job.github.installation_token.assert_not_awaited()
    job.github.create_pull_request.assert_not_awaited()
    job.command.assert_not_awaited()
    await assert_quarantined(job)


@pytest.mark.parametrize("stage", ["metadata", "clone", "commit", "push"])
async def test_inflight_quarantine_blocks_subsequent_publication(pending_delivery, stage):
    job = pending_delivery

    async def metadata(*_):
        if stage == "metadata":
            await quarantine(job)
        return {"default_branch": "main"}

    async def command(*args, **_):
        if args[:2] == ("git", stage):
            await quarantine(job)
        return ""

    job.github.repository.side_effect = metadata
    job.command.side_effect = command
    await delivery.deliver_work(job.work.id)
    job.github.create_pull_request.assert_not_awaited()
    pushes = [call for call in job.command.await_args_list if call.args[:2] == ("git", "push")]
    assert len(pushes) == (1 if stage == "push" else 0)
    await assert_quarantined(job)


async def test_late_delivery_failure_preserves_committed_quarantine(pending_delivery):
    job = pending_delivery

    async def fail(*_, **__):
        await quarantine(job)
        raise RuntimeError("synthetic late clone failure")

    job.command.side_effect = fail
    await delivery.deliver_work(job.work.id)
    job.github.create_pull_request.assert_not_awaited()
    await assert_quarantined(job)


@pytest.mark.parametrize("existing", ["none", "branch", "pull_request"])
async def test_unquarantined_delivery_completes_without_duplicate_publication(
    pending_delivery, existing,
):
    job = pending_delivery
    url = "https://github.com/acme/test/pull/1"
    job.github.branch_exists.return_value = existing == "branch"
    job.github.find_pull_request.return_value = url if existing == "pull_request" else None
    await delivery.deliver_work(job.work.id)
    async with job.sessions() as session:
        work = await session.get(WorkItem, job.work.id)
        assert work.status == WorkStatus.COMPLETED and work.pull_request_url == url
        assert (await session.get(DeliveryJob, job.work.id)).state == "completed"
    assert job.github.create_pull_request.await_count == (0 if existing == "pull_request" else 1)
    pushes = [call for call in job.command.await_args_list if call.args[:2] == ("git", "push")]
    assert len(pushes) == (1 if existing == "none" else 0)


async def test_normal_delivery_failure_is_recorded(pending_delivery):
    job = pending_delivery
    job.command.side_effect = RuntimeError("synthetic clone failure")
    await delivery.deliver_work(job.work.id)
    async with job.sessions() as session:
        assert (await session.get(WorkItem, job.work.id)).status == WorkStatus.FAILED
        assert (await session.get(DeliveryJob, job.work.id)).state == "failed"
        events = (await session.scalars(select(AgentEvent.event_type))).all()
        assert events.count("delivery.failed") == 1


async def test_delivery_write_has_a_bounded_deadline(pending_delivery, monkeypatch):
    job = pending_delivery
    monkeypatch.setattr(delivery, "DELIVERY_WRITE_SECONDS", 0.02)

    async def stalled(*_):
        await asyncio.Event().wait()

    job.github.installation_token.side_effect = stalled
    async with asyncio.timeout(2):
        await delivery.deliver_work(job.work.id)
    job.github.create_pull_request.assert_not_awaited()
    async with job.sessions() as session:
        assert (await session.get(DeliveryJob, job.work.id)).state == "failed"
