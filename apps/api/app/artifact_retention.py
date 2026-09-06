"""Journaled ordinary-file retention for the control-host CLI, not an API background task."""

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from .artifact_retention_files import RetentionFileError, inspect_file, purge_file
from .models import (
    Artifact,
    AuditRecord,
    ConsoleLease,
    DeliveryJob,
    PreviewEndpoint,
    ResourceLease,
    WorkerHost,
    WorkItem,
    WorkStatus,
    utcnow,
)

MAX_ALIASES = 10_000


class ProtectedArtifact(RuntimeError):
    """Fixed guard reason; protects data without modifying execution resources."""


@dataclass(frozen=True)
class RetentionResult:
    status: str
    reason: str | None = None
    aliases: int = 0
    bytes_removed: int = 0


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def canonical_id(value: str) -> str:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("canonical UUID required")
    return value


def policy_days(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 36500:
        raise ValueError("retention days must be between 1 and 36500")
    return value


def explicit_apply(value: bool) -> bool:
    if type(value) is not bool:
        raise ValueError("apply must be an explicit boolean")
    return value


@asynccontextmanager
async def transaction(sessions):
    async with sessions() as session, session.begin():
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            await session.execute(text("BEGIN IMMEDIATE"))
        elif dialect == "postgresql":
            await session.execute(text("SET LOCAL lock_timeout = '2s'"))
            await session.execute(text("SET LOCAL statement_timeout = '15s'"))
        else:
            raise ProtectedArtifact("unsupported_database")
        yield session


def owners(work, lease):
    return {value for value in (work.assigned_worker_id, lease.worker_id if lease else None)
            if value is not None}


async def locked_group(session, identity: str, days: int, now: datetime):
    seed = await session.get(Artifact, identity)
    if seed is None:
        raise ProtectedArtifact("artifact_missing")
    work = await session.get(WorkItem, seed.work_item_id)
    if work is None:
        raise ProtectedArtifact("work_missing")
    lease_query = select(ResourceLease).where(ResourceLease.work_item_id == work.id)
    lease = await session.scalar(lease_query)
    initial_owners = owners(work, lease)
    # Same order as lease validation, Claim and Worker quarantine. Never acquire
    # a Worker lock after Work: changed ownership means retry, not reverse locking.
    for owner in sorted(initial_owners):
        worker = await session.get(WorkerHost, owner, with_for_update=True, populate_existing=True)
        if worker is None or worker.quarantined_at is not None:
            raise ProtectedArtifact("worker_quarantined_or_missing")
    lease = await session.scalar(lease_query.with_for_update()
                                 .execution_options(populate_existing=True))
    work = await session.get(WorkItem, work.id, with_for_update=True, populate_existing=True)
    if work is None or owners(work, lease) != initial_owners:
        raise ProtectedArtifact("ownership_changed")
    if ((lease is None and work.assigned_worker_id is not None)
            or (lease is not None and lease.worker_id != work.assigned_worker_id)):
        raise ProtectedArtifact("inconsistent_lease_owner")
    if lease is not None and lease.state != "released":
        raise ProtectedArtifact("lease_not_released")
    if work.status not in {WorkStatus.COMPLETED, WorkStatus.CANCELLED}:
        raise ProtectedArtifact("work_not_final")
    cutoff = now - timedelta(days=days)
    if aware(work.updated_at) > cutoff:
        raise ProtectedArtifact("recent_work")
    job = await session.get(DeliveryJob, work.id, with_for_update=True, populate_existing=True)
    if job is not None and job.state != "completed":
        raise ProtectedArtifact("delivery_not_final")
    preview = await session.scalar(select(PreviewEndpoint).where(
        PreviewEndpoint.work_item_id == work.id).with_for_update()
        .execution_options(populate_existing=True))
    console = await session.get(ConsoleLease, work.id, with_for_update=True, populate_existing=True)
    if preview is not None and aware(preview.expires_at) > now:
        raise ProtectedArtifact("active_preview")
    if console is not None and aware(console.expires_at) > now:
        raise ProtectedArtifact("active_console")
    rows = list((await session.scalars(select(Artifact).where(
        Artifact.object_key == seed.object_key).order_by(Artifact.id).limit(MAX_ALIASES + 1)
        .with_for_update().execution_options(populate_existing=True))).all())
    if not rows or len(rows) > MAX_ALIASES or identity not in {row.id for row in rows}:
        raise ProtectedArtifact("alias_set_changed_or_too_large")
    if any(row.work_item_id != work.id for row in rows):
        raise ProtectedArtifact("foreign_work_alias")
    if any(aware(row.created_at) > cutoff for row in rows):
        raise ProtectedArtifact("recent_artifact")
    states = {(row.expired_at, row.purged_at, row.retention_days, row.retention_sha256,
               row.size_bytes) for row in rows}
    if len(states) != 1:
        raise ProtectedArtifact("inconsistent_aliases")
    first = rows[0]
    if first.expired_at is not None:
        if first.retention_days != days:
            raise ProtectedArtifact("retention_policy_changed")
        if aware(first.expired_at) > now:
            raise ProtectedArtifact("future_expiration")
    return work, rows


def record_audit(session, work, row, action: str, request_id: str):
    session.add(AuditRecord(
        organization_id=work.organization_id, work_item_id=work.id, repository=work.repository,
        action=action, target_id=row.id, actor_id=None, actor_subject="artifact:retention",
        identity_provider="urn:kelpie:service", organization_role=None, repository_role=None,
        effective_role=None, required_role=None, source_ip=None, transport="background",
        request_id=request_id, correlation_id=work.correlation_id, details={
            "retention_days": row.retention_days, "sha256": row.retention_sha256,
            "size_bytes": row.size_bytes, "work_status": work.status.value,
            "work_version": work.version,
        },
    ))


def intent(rows):
    return tuple((row.id, row.work_item_id, row.object_key, row.size_bytes,
                  aware(row.expired_at), row.retention_days, row.retention_sha256) for row in rows)


async def expire_artifact(sessions, root: Path, identity: str, *, retain_days: int,
                          apply: bool = False, now: datetime | None = None) -> RetentionResult:
    canonical_id(identity)
    policy_days(retain_days)
    explicit_apply(apply)
    now = aware(now or utcnow())
    request_id = str(uuid.uuid4())
    try:
        async with transaction(sessions) as session:
            work, rows = await locked_group(session, identity, retain_days, now)
            row = rows[0]
            if row.purged_at is not None:
                return RetentionResult("already_purged")
            # Deliberately bounded synchronous IO in this CLI-only process: cancellation
            # cannot release DB fences while an unjoined file-deletion thread keeps running.
            sha = inspect_file(root, work.id, row.object_key, row.size_bytes,
                expected=row.retention_sha256, missing_ok=row.expired_at is not None)
            if not apply:
                return RetentionResult("eligible", aliases=len(rows))
            if row.expired_at is None:
                for alias in rows:
                    alias.expired_at = now
                    alias.retention_days = retain_days
                    alias.retention_sha256 = sha
                    record_audit(session, work, alias, "artifact.expiration_requested", request_id)
            expected_intent = intent(rows)
        # The expiration intent is durable before touching bytes. New API downloads
        # reject it, and a crash leaves evidence for an idempotent second-phase retry.
        async with transaction(sessions) as session:
            work, rows = await locked_group(session, identity, retain_days, now)
            row = rows[0]
            if row.purged_at is not None:
                return RetentionResult("already_purged")
            if any(alias.expired_at is None for alias in rows) or intent(rows) != expected_intent:
                raise ProtectedArtifact("expiration_intent_changed")
            removed = purge_file(root, work.id, row.object_key, row.size_bytes,
                                 row.retention_sha256)
            completed_at = max(now, utcnow())
            for alias in rows:
                alias.purged_at = completed_at
                record_audit(session, work, alias, "artifact.purged", request_id)
            result = RetentionResult("purged", aliases=len(rows),
                                     bytes_removed=row.size_bytes if removed else 0)
        return result
    except ProtectedArtifact as error:
        return RetentionResult("protected", reason=str(error))
    except RetentionFileError as error:
        return RetentionResult("failed", reason=str(error))
    except SQLAlchemyError:
        return RetentionResult("failed", reason="database_unavailable")
