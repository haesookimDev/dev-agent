import asyncio
import uuid

import pytest
from delivery_fixtures import PATCH_SHA256
from sqlalchemy import delete, select, text
from test_delivery_quarantine import pending_delivery as pending_delivery
from test_delivery_quarantine import quarantine
from test_worker_credentials import database as database

from app import delivery
from app.delivery_audit import pull_request_number
from app.models import AgentEvent, AuditRecord, DeliveryBundle, DeliveryJob, WorkItem, WorkStatus


async def records(job):
    async with job.sessions() as session:
        return list(await session.scalars(select(AuditRecord).where(
            AuditRecord.work_item_id == job.work.id,
        ).order_by(AuditRecord.id)))


@pytest.mark.parametrize("existing", ["none", "branch", "pull_request"])
async def test_delivery_audits_service_execution_and_original_approval(pending_delivery, existing):
    job = pending_delivery
    url = "https://github.com/acme/test/pull/1"
    job.github.find_pull_request.return_value = url if existing == "pull_request" else None
    job.github.branch_exists.return_value = existing == "branch"
    await delivery.deliver_work(job.work.id)
    source, started, completed = await records(job)
    assert [row.action for row in (source, started, completed)] == [
        "approval.decided", "delivery.started", "delivery.completed",
    ]
    assert source.actor_subject == "original-approver"
    assert source.identity_provider == "https://identity.example"
    assert source.required_role == "approver"
    assert started.request_id == completed.request_id != source.request_id
    uuid.UUID(started.request_id)
    for row in (started, completed):
        assert row.actor_subject == "delivery:github"
        assert row.identity_provider == "urn:kelpie:service"
        assert row.transport == "background"
        assert row.actor_id is row.source_ip is row.required_role is None
        assert row.organization_role is row.repository_role is row.effective_role is None
        assert row.organization_id == source.organization_id
        assert row.correlation_id == source.correlation_id
        assert row.repository == source.repository
        assert row.details["approval_audit_id"] == source.id
        assert row.details["approved_bundle_sha256"] == PATCH_SHA256
        assert row.details["approved_work_version"] == source.details["work_version_after"]
        assert row.details["authorization"] == "verified"
        assert row.details["attempt"] == 1
        assert "synthetic-delivery-token" not in str(row.details)
        assert "verified.patch" not in str(row.details)
    assert started.details["work_status"] == "committing"
    assert completed.details["work_status"] == "completed"
    assert completed.details["work_version"] == source.details["work_version_after"] + 2
    assert completed.details["publication"] == {
        "none": "new_branch", "branch": "existing_branch", "pull_request": "existing_pull_request",
    }[existing]
    assert completed.details["pull_request_number"] == 1
    # A duplicate request must not add another service attempt or audit.
    await delivery.deliver_work(job.work.id)
    assert len(await records(job)) == 3


@pytest.mark.parametrize("change", [
    {"organization_id": "another-org"}, {"repository": "another/repository"},
    {"work_item_id": "another-work"}, {"correlation_id": "another-correlation"},
    {"action": "feedback.created"}, {"effective_role": "viewer"},
    {"required_role": "operator"}, {"details": []},
    {"details": {"decision": "reject"}}, {"details": {"kind": "budget"}},
    {"details": {"delivery_queued": False}}, {"details": {"work_status_after": "failed"}},
    {"details": {"work_version_after": True}}, {"details": {"work_version_after": 99}},
    {"details": {"delivery_bundle_sha256": "b" * 64}},
])
async def test_invalid_approval_evidence_blocks_every_external_action(pending_delivery, change):
    job = pending_delivery
    async with job.sessions() as session:
        source = await session.get(AuditRecord, job.approval.id)
        values = {column.name: getattr(source, column.name) for column in source.__table__.columns
                  if column.name != "id"}
        if "details" in change and isinstance(change["details"], dict):
            change = change | {"details": source.details | change["details"]}
        different = AuditRecord(**(values | change | {"actor_subject": "private-foreign-approver"}))
        session.add(different)
        await session.flush()
        (await session.get(DeliveryJob, job.work.id)).approval_audit_id = different.id
        await session.commit()
    await delivery.deliver_work(job.work.id)
    job.github.installation_token.assert_not_awaited()
    job.github.create_pull_request.assert_not_awaited()
    job.command.assert_not_awaited()
    outcomes = [row for row in await records(job) if row.transport == "background"]
    assert len(outcomes) == 1 and outcomes[0].action == "delivery.failed"
    assert outcomes[0].details["error_code"] == "approval_mismatch"
    assert outcomes[0].details["stage"] == "authorization"
    assert outcomes[0].details["authorization"] == "denied"
    assert outcomes[0].details["approval_audit_id"] is None
    assert outcomes[0].details["approved_bundle_sha256"] is None
    assert "private-foreign" not in str(outcomes[0].details)


@pytest.mark.parametrize("missing,code", [
    ("link", "approval_unavailable"), ("source", "approval_unavailable"),
    ("bundle", "bundle_unavailable"), ("digest", "approval_mismatch"),
])
async def test_unattributed_legacy_or_changed_bundles_fail_closed(pending_delivery, missing, code):
    job = pending_delivery
    async with job.sessions() as session:
        if missing in {"link", "source"}:
            (await session.get(DeliveryJob, job.work.id)).approval_audit_id = (
                None if missing == "link" else 2147483647
            )
        elif missing == "bundle":
            await session.execute(delete(DeliveryBundle).where(
                DeliveryBundle.work_item_id == job.work.id,
            ))
        else:
            (await session.get(DeliveryBundle, job.work.id)).sha256 = "b" * 64
        await session.commit()
    await delivery.resume_pending_deliveries()
    source, failure = await records(job)
    assert source.action == "approval.decided"
    assert failure.action == "delivery.failed"
    assert failure.details["error_code"] == code
    assert failure.details["authorization"] == "denied"
    assert failure.details["publication"] == "not_started"
    job.github.installation_token.assert_not_awaited()
    job.command.assert_not_awaited()


async def test_start_audit_failure_rolls_back_without_issuing_a_token(pending_delivery):
    job = pending_delivery
    async with job.sessions() as session:
        await session.execute(text(
            "CREATE TRIGGER unavailable_delivery_audit BEFORE INSERT ON audit_records "
            "WHEN NEW.transport = 'background' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic-private-audit-error'); END"
        ))
        await session.commit()
    with pytest.raises(delivery.DeliveryPersistenceError, match="recovery required") as error:
        await delivery.deliver_work(job.work.id)
    assert "synthetic-private" not in str(error.value)
    job.github.installation_token.assert_not_awaited()
    job.command.assert_not_awaited()
    async with job.sessions() as session:
        state = await session.get(DeliveryJob, job.work.id)
        assert (state.state, state.attempts) == ("pending", 0)
        assert (await session.get(WorkItem, job.work.id)).status == WorkStatus.COMMITTING
        assert not list(await session.scalars(select(AgentEvent)))
        await session.execute(text("DROP TRIGGER unavailable_delivery_audit"))
        await session.commit()
    assert len(await records(job)) == 1
    await delivery.deliver_work(job.work.id)
    assert (await records(job))[-1].action == "delivery.completed"


async def test_completion_audit_failure_recovers_the_existing_pr_without_a_second_write(
    pending_delivery,
):
    job = pending_delivery
    async with job.sessions() as session:
        await session.execute(text(
            "CREATE TRIGGER completion_audit_unavailable BEFORE INSERT ON audit_records "
            "WHEN NEW.action = 'delivery.completed' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic audit unavailable'); END"
        ))
        await session.commit()
    with pytest.raises(delivery.DeliveryPersistenceError):
        await delivery.deliver_work(job.work.id)
    async with job.sessions() as session:
        assert (await session.get(WorkItem, job.work.id)).status == WorkStatus.COMMITTING
        job_state = await session.get(DeliveryJob, job.work.id)
        assert (job_state.state, job_state.attempts) == ("running", 1)
        assert not list(await session.scalars(select(AgentEvent)))
        await session.execute(text("DROP TRIGGER completion_audit_unavailable"))
        await session.commit()
    job.github.create_pull_request.assert_awaited_once()
    assert [r.action for r in await records(job)] == ["approval.decided", "delivery.started"]
    job.github.find_pull_request.return_value = "https://github.com/acme/test/pull/1"
    await delivery.resume_pending_deliveries()
    audits = await records(job)
    assert [r.action for r in audits] == [
        "approval.decided", "delivery.started", "delivery.interrupted",
        "delivery.started", "delivery.completed",
    ]
    assert [r.details["attempt"] for r in audits[1:]] == [1, 1, 2, 2]
    assert audits[1].request_id != audits[3].request_id == audits[4].request_id
    assert audits[2].details["publication"] == "unknown"
    assert audits[-1].details["publication"] == "existing_pull_request"
    job.github.create_pull_request.assert_awaited_once()


async def test_inflight_quarantine_is_audited_without_rewriting_its_terminal_state(
    pending_delivery,
):
    job = pending_delivery

    async def metadata(*_):
        await quarantine(job)
        return {"default_branch": "main"}

    job.github.repository.side_effect = metadata
    await delivery.deliver_work(job.work.id)
    source, started, stopped = await records(job)
    assert stopped.action == "delivery.stopped"
    assert stopped.request_id == started.request_id
    assert stopped.details["approval_audit_id"] == source.id
    assert stopped.details["job_state"] == "quarantined"
    assert stopped.details["work_status"] == "failed"
    assert stopped.details["error_code"] == "execution_fenced"
    job.github.create_pull_request.assert_not_awaited()


async def test_publication_rechecks_the_approved_work_version(pending_delivery):
    job = pending_delivery

    async def metadata(*_):
        async with job.sessions() as session:
            (await session.get(WorkItem, job.work.id)).version += 1
            await session.commit()
        return {"default_branch": "main"}

    job.github.repository.side_effect = metadata
    await delivery.deliver_work(job.work.id)
    source, started, failure = await records(job)
    assert failure.action == "delivery.failed"
    assert failure.details["error_code"] == "approval_mismatch"
    assert failure.details["authorization"] == "denied"
    assert failure.details["approval_audit_id"] == source.id
    assert failure.request_id == started.request_id
    assert not any(call.args[:2] == ("git", "push") for call in job.command.await_args_list)
    job.github.create_pull_request.assert_not_awaited()


async def test_duplicate_concurrent_delivery_has_one_durable_attempt(pending_delivery):
    job = pending_delivery
    started, release = asyncio.Event(), asyncio.Event()

    async def metadata(*_):
        started.set()
        await release.wait()
        return {"default_branch": "main"}

    job.github.repository.side_effect = metadata
    first = asyncio.create_task(delivery.deliver_work(job.work.id))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await delivery.deliver_work(job.work.id)
        await delivery.resume_pending_deliveries()
        release.set()
        await asyncio.wait_for(first, 2)
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    assert [r.action for r in await records(job)] == [
        "approval.decided", "delivery.started", "delivery.completed",
    ]
    job.github.create_pull_request.assert_awaited_once()


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/acme/test/pull/42", 42),
    ("https://github.com/acme/test/pull/1?token=private", None),
    ("https://github.com/acme/other/pull/1", None),
    ("https://private.example/acme/test/pull/1", None),
    ("https://github.com/acme/test/pull/0", None), (None, None),
])
def test_only_bounded_repository_pr_numbers_are_copied_into_audits(url, expected):
    assert pull_request_number(url, "acme/test") == expected
