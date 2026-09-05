"""Control-plane quarantine. Physical host/VM isolation remains an operator action."""

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    ConsoleLease,
    DeliveryJob,
    PreviewEndpoint,
    ResourceLease,
    WorkerCredential,
    WorkerHost,
    WorkerState,
    WorkItem,
    WorkStatus,
    utcnow,
)
from .schemas import EventCreate
from .service import emit_event, transition_work_item
from .worker_credentials import lock_worker, record_event, validate_audit

TERMINAL_WORK = {WorkStatus.COMPLETED, WorkStatus.CANCELLED, WorkStatus.FAILED}
QUARANTINE_MESSAGE = "Worker quarantined; control-plane access revoked; physical cleanup required"


@dataclass(frozen=True)
class QuarantineResult:
    worker_id: str
    already_quarantined: bool
    revoked_credentials: int
    invalidated_leases: int
    affected_work_ids: tuple[str, ...]


async def ensure_worker_not_quarantined(session: AsyncSession, work: WorkItem) -> None:
    """Caller holds the Work row lock; never acquire a Worker lock in this order."""
    if work.assigned_worker_id is None:
        return
    quarantined_at = await session.scalar(select(WorkerHost.quarantined_at)
                                          .where(WorkerHost.id == work.assigned_worker_id))
    if quarantined_at is not None:
        raise HTTPException(409, "work's worker is quarantined")


async def quarantine_worker(
    session: AsyncSession, worker_id: str, *, actor: str, reason: str,
) -> QuarantineResult:
    validate_audit(actor, reason)
    worker = await lock_worker(session, worker_id)
    if worker.quarantined_at is not None:
        return QuarantineResult(worker.id, True, 0, 0, ())
    now = utcnow()
    worker.quarantined_at = now
    worker.credential_required = True
    worker.state = WorkerState.OFFLINE
    credentials = list((await session.scalars(select(WorkerCredential).where(
        WorkerCredential.worker_id == worker.id, WorkerCredential.revoked_at.is_(None),
    ).order_by(WorkerCredential.id).with_for_update())).all())
    for credential in credentials:
        credential.revoked_at = now
        record_event(session, worker.id, credential.id, "revoked", actor, reason)
    leases = list((await session.scalars(select(ResourceLease).where(
        ResourceLease.worker_id == worker.id, ResourceLease.state == "active",
    ).order_by(ResourceLease.id).with_for_update())).all())
    for lease in leases:
        lease.state = "quarantined"
        lease.expires_at = now
    # Keep reservations held: revocation is not proof that the VM has stopped.
    works = list((await session.scalars(select(WorkItem).where(
        WorkItem.assigned_worker_id == worker.id,
    ).order_by(WorkItem.id).with_for_update()
      .execution_options(populate_existing=True))).all())
    for work in works:
        job = await session.get(DeliveryJob, work.id, with_for_update=True)
        if job is not None and job.state in {"pending", "retry", "running"}:
            job.state = "quarantined"
            job.error = "worker quarantined; delivery blocked"
            job.updated_at = now
        preview = await session.scalar(select(PreviewEndpoint).where(
            PreviewEndpoint.work_item_id == work.id,
        ).with_for_update())
        if preview is not None:
            preview.expires_at = now
        console = await session.get(ConsoleLease, work.id, with_for_update=True)
        if console is not None:
            console.expires_at = now
            console.version += 1
        if work.status not in TERMINAL_WORK:
            target = (WorkStatus.FAILED if work.status in {
                WorkStatus.COMMITTING, WorkStatus.PR_CREATED,
            } else WorkStatus.CANCELLED)
            await transition_work_item(session, work, target, expected_version=work.version,
                                       actor="worker-admin", message=QUARANTINE_MESSAGE)
        await emit_event(session, work.id, EventCreate(
            event_type="worker.quarantined", source="worker-admin", level="error",
            message=QUARANTINE_MESSAGE, payload={"worker_id": worker.id},
        ))
    record_event(session, worker.id, None, "quarantined", actor, reason)
    await session.flush()
    return QuarantineResult(worker.id, False, len(credentials), len(leases),
                            tuple(work.id for work in works))
