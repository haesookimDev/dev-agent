import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from artifact_retention_case import seed
from sqlalchemy import delete, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import artifact_retention as retention
from app import artifact_retention_files as files
from app import models as m


@pytest.fixture
async def case(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}")
    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(m.Base.metadata.create_all)
        yield await seed(async_sessionmaker(engine, expire_on_commit=False), tmp_path / "files")
    finally:
        await engine.dispose()


async def assert_untouched(case, reason, **kwargs):
    before = await case.snapshot()
    result = await case.expire(**kwargs)
    assert result == retention.RetentionResult("protected", reason=reason)
    assert await case.snapshot() == before
    assert case.path.read_bytes() == case.content


async def test_dry_run_is_read_only_and_has_no_default_policy(case):
    before = await case.snapshot()
    result = await case.expire(apply=False)
    assert result == retention.RetentionResult("eligible", aliases=1)
    assert await case.snapshot() == before
    assert case.path.read_bytes() == case.content
    with pytest.raises(TypeError):
        await retention.expire_artifact(case.sessions, case.root, case.artifact)


@pytest.mark.parametrize("days", [0, -1, 36501, True, 1.5, "30", None])
async def test_invalid_policy_cannot_mutate_state(case, days):
    before = await case.snapshot()
    with pytest.raises(ValueError):
        await case.expire(retain_days=days)
    assert await case.snapshot() == before and case.path.read_bytes() == case.content


@pytest.mark.parametrize("status", [m.WorkStatus.COMPLETED, m.WorkStatus.CANCELLED])
async def test_final_work_purges_all_aliases_once_and_preserves_execution_resources(case, status):
    alias = await case.alias(name="second retained name")
    async with case.sessions() as session:
        work = await session.get(m.WorkItem, case.work)
        work.status = status
        work.updated_at = m.utcnow() - timedelta(days=40)
        session.add(m.DeliveryJob(work_item_id=work.id, state="completed"))
        await session.commit()
    before = await case.snapshot()
    result = await case.expire()
    assert result == retention.RetentionResult("purged", aliases=2, bytes_removed=len(case.content))
    assert not case.path.exists()
    assert (case.root / case.work / "delivery.patch").read_bytes() == b"Synthetic delivery evidence"
    rows, audits = await case.evidence()
    assert {row.id for row in rows} == {case.artifact, alias}
    assert all(row.purged_at >= row.expired_at and row.retention_days == 30
               and row.retention_sha256 == case.digest for row in rows)
    assert len(audits) == 4
    for identity in (case.artifact, alias):
        events = [audit for audit in audits if audit.target_id == identity]
        assert [audit.action for audit in events] == [
            "artifact.expiration_requested", "artifact.purged"]
        assert len({audit.request_id for audit in events}) == 1
        assert all(audit.work_item_id == case.work and audit.actor_id is None
            and audit.actor_subject == "artifact:retention"
            and audit.identity_provider == "urn:kelpie:service" and audit.transport == "background"
            and audit.details == {"retention_days": 30, "sha256": case.digest,
                "size_bytes": len(case.content), "work_status": status.value, "work_version": 1}
            for audit in events)
    after = await case.snapshot()
    for table in before.keys() - {"artifacts", "audit_records"}:
        assert after[table] == before[table]
    assert (await case.expire()).status == "already_purged"
    assert await case.snapshot() == after


@pytest.mark.parametrize("status", [value for value in m.WorkStatus if value not in {
    m.WorkStatus.COMPLETED, m.WorkStatus.CANCELLED}])
async def test_nonfinal_and_retryable_failed_work_are_protected(case, status):
    async with case.sessions() as session:
        (await session.get(m.WorkItem, case.work)).status = status
        await session.commit()
    await assert_untouched(case, "work_not_final")


@pytest.mark.parametrize("state", ["active", "quarantined", "expired", "unknown"])
async def test_only_explicitly_released_lease_is_eligible_even_after_clock_expiration(case, state):
    async with case.sessions() as session:
        (await session.get(m.ResourceLease, case.lease)).state = state
        await session.commit()
    await assert_untouched(case, "lease_not_released")


@pytest.mark.parametrize("guard", ["quarantine", "unassigned", "missing-lease", "recent-work",
    "recent-artifact", "preview", "console", "pending", "retry", "running", "failed"])
async def test_live_activity_and_inconsistent_ownership_protect_artifacts(case, guard):
    reasons = {"quarantine": "worker_quarantined_or_missing",
        "unassigned": "inconsistent_lease_owner", "missing-lease": "inconsistent_lease_owner",
        "recent-work": "recent_work", "recent-artifact": "recent_artifact",
        "preview": "active_preview", "console": "active_console"}
    async with case.sessions() as session:
        work = await session.get(m.WorkItem, case.work)
        if guard == "quarantine":
            (await session.get(m.WorkerHost, case.worker)).quarantined_at = m.utcnow()
        elif guard == "unassigned":
            work.assigned_worker_id = None
        elif guard == "missing-lease":
            await session.execute(delete(m.ResourceLease).where(m.ResourceLease.id == case.lease))
        elif guard == "recent-work":
            work.updated_at = m.utcnow()
        elif guard == "recent-artifact":
            await case.alias(created_at=m.utcnow())
        elif guard == "preview":
            session.add(m.PreviewEndpoint(work_item_id=case.work, hostname="synthetic.invalid",
                target_url="http://127.0.0.1:18000", expires_at=m.utcnow() + timedelta(minutes=1)))
        elif guard == "console":
            session.add(m.ConsoleLease(work_item_id=case.work,
                                       expires_at=m.utcnow() + timedelta(minutes=1)))
        else:
            session.add(m.DeliveryJob(work_item_id=case.work, state=guard))
        await session.commit()
    await assert_untouched(case, reasons.get(guard, "delivery_not_final"))


async def test_never_assigned_cancelled_work_and_expired_endpoints_are_eligible(case):
    async with case.sessions() as session:
        await session.execute(delete(m.ResourceLease).where(m.ResourceLease.id == case.lease))
        work = await session.get(m.WorkItem, case.work)
        work.assigned_worker_id = None
        work.status = m.WorkStatus.CANCELLED
        work.updated_at = m.utcnow() - timedelta(days=40)
        past = m.utcnow() - timedelta(seconds=1)
        session.add_all([m.ConsoleLease(work_item_id=case.work, expires_at=past),
            m.PreviewEndpoint(work_item_id=case.work, hostname="synthetic.invalid",
                              target_url="http://127.0.0.1:18000", expires_at=past)])
        await session.commit()
    assert (await case.expire()).status == "purged"


@pytest.mark.parametrize("change", ["size", "expiration", "foreign", "bound"])
async def test_ambiguous_or_unbounded_aliases_never_delete_content(case, monkeypatch, change):
    kwargs = {}
    reason = "inconsistent_aliases"
    if change == "size":
        kwargs["size_bytes"] = 1
    elif change == "expiration":
        kwargs.update(expired_at=m.utcnow(), retention_days=30, retention_sha256=case.digest)
    elif change == "foreign":
        async with case.sessions() as session:
            work = await session.get(m.WorkItem, case.work)
            foreign = m.WorkItem(organization_id=work.organization_id, source=m.WorkSource.WEB,
                title="Different run", requirement="Never expire by alias",
                repository=work.repository)
            session.add(foreign)
            await session.commit()
            kwargs["work_item_id"] = foreign.id
        reason = "foreign_work_alias"
    else:
        monkeypatch.setattr(retention, "MAX_ALIASES", 1)
        reason = "alias_set_changed_or_too_large"
    await case.alias(**kwargs)
    await assert_untouched(case, reason)


@pytest.mark.parametrize("phase", ["intent", "unlink", "fsync", "completion"])
async def test_crash_boundaries_keep_durable_intent_and_retry_without_duplicate_audits(
    case, monkeypatch, phase,
):
    original = retention.record_audit
    def fail_audit(session, work, row, action, request_id):
        if (phase == "intent" and action == "artifact.expiration_requested"
                or phase == "completion" and action == "artifact.purged"):
            raise SQLAlchemyError("synthetic private database failure")
        return original(session, work, row, action, request_id)
    def failed_io(*_, **__):
        raise OSError("synthetic private storage failure")
    with monkeypatch.context() as patch:
        patch.setattr(retention, "record_audit", fail_audit)
        if phase in {"unlink", "fsync"}:
            patch.setattr(files.os, phase, failed_io)
        result = await case.expire()
    assert result.status == "failed"
    rows, audits = await case.evidence()
    assert rows[0].purged_at is None
    assert (rows[0].expired_at is None) == (phase == "intent")
    assert len(audits) == (0 if phase == "intent" else 1)
    assert case.path.exists() == (phase in {"intent", "unlink"})
    result = await case.expire()
    assert result.status == "purged"
    assert result.bytes_removed == (len(case.content) if phase in {"intent", "unlink"} else 0)
    rows, audits = await case.evidence()
    assert rows[0].purged_at is not None
    assert [audit.action for audit in audits] == [
        "artifact.expiration_requested", "artifact.purged"]
    if phase != "intent":
        assert audits[0].request_id != audits[1].request_id


@pytest.mark.parametrize("change", ["worker", "delivery", "new-alias"])
async def test_guards_are_reacquired_after_committed_intent_before_unlink(
    case, monkeypatch, change,
):
    original = retention.transaction
    calls = 0
    @asynccontextmanager
    async def changing(sessions):
        nonlocal calls
        calls += 1
        if calls == 2:
            # A separate connection commits in the real gap between the two transactions.
            async with sessions() as session:
                if change == "worker":
                    (await session.get(m.WorkerHost, case.worker)).quarantined_at = m.utcnow()
                elif change == "delivery":
                    session.add(m.DeliveryJob(work_item_id=case.work, state="retry"))
                else:
                    await case.alias()
                await session.commit()
        async with original(sessions) as session:
            yield session
    monkeypatch.setattr(retention, "transaction", changing)
    result = await case.expire()
    assert result.status == "protected"
    assert result.reason == {"worker": "worker_quarantined_or_missing",
        "delivery": "delivery_not_final", "new-alias": "expiration_intent_changed"}[change]
    rows, audits = await case.evidence()
    assert all(row.expired_at is not None and row.purged_at is None for row in rows)
    assert len(audits) == 1
    assert case.path.read_bytes() == case.content


async def test_concurrent_alias_cleanup_serializes_and_records_each_phase_once(case):
    alias = await case.alias()
    results = await asyncio.gather(case.expire(), retention.expire_artifact(
        case.sessions, case.root, alias, retain_days=30, apply=True))
    assert sorted(result.status for result in results) == ["already_purged", "purged"]
    assert sum(result.bytes_removed for result in results) == len(case.content)
    rows, audits = await case.evidence()
    assert len(rows) == 2 and all(row.purged_at is not None for row in rows)
    assert len(audits) == 4


@pytest.mark.parametrize("change,reason", [
    ({"retention_days": 31}, "retention_policy_changed"),
    ({"expired_at": m.utcnow() + timedelta(days=1)}, "future_expiration"),
])
async def test_pending_intent_is_not_reinterpreted_as_a_new_policy(case, change, reason):
    async with case.sessions() as session:
        row = await session.get(m.Artifact, case.artifact)
        row.expired_at, row.retention_days, row.retention_sha256 = m.utcnow(), 30, case.digest
        for name, value in change.items():
            setattr(row, name, value)
        await session.commit()
    await assert_untouched(case, reason)
