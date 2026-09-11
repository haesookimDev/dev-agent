"""Individual Worker recovery authority, without recreating lost run tokens."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .correlation import current_request_id
from .models import AuditRecord, DeliveryJob, ResourceLease, WorkerHost, WorkItem, WorkStatus
from .schemas import EventCreate, LeaseReconciliationRequest, WorkerLeaseView
from .service import emit_event

TERMINAL_WORK = {WorkStatus.COMPLETED, WorkStatus.FAILED, WorkStatus.CANCELLED}


async def owned_lease(
    session: AsyncSession, worker: WorkerHost, lease_id: str,
) -> tuple[ResourceLease, WorkItem]:
    # The caller's individual authentication holds Worker -> Credential locks.
    # Keep the shared Worker -> Lease -> Work order used by release/quarantine.
    if worker.quarantined_at is not None:
        raise HTTPException(403, "worker is quarantined")
    lease = await session.scalar(select(ResourceLease).where(
        ResourceLease.id == lease_id, ResourceLease.worker_id == worker.id,
    ).with_for_update().execution_options(populate_existing=True))
    if lease is None:
        raise HTTPException(404, "lease not found")
    item = await session.scalar(select(WorkItem).where(
        WorkItem.id == lease.work_item_id,
    ).with_for_update().execution_options(populate_existing=True))
    if item is None or item.assigned_worker_id != worker.id:
        raise HTTPException(409, "lease assignment no longer matches worker")
    return lease, item


def lease_view(lease: ResourceLease, item: WorkItem) -> WorkerLeaseView:
    # Do not return credentials, token hashes, repository data or user input.
    return WorkerLeaseView(
        lease_id=lease.id, worker_id=lease.worker_id, work_item_id=item.id,
        state=lease.state, work_status=item.status, work_version=item.version,
        cpu=lease.cpu, memory_mb=lease.memory_mb, disk_gb=lease.disk_gb,
    )


def return_lease_resources(worker: WorkerHost, lease: ResourceLease) -> None:
    """Caller holds Worker/Lease locks and has checked active, terminal ownership."""
    worker.cpu_available = min(worker.cpu_total, worker.cpu_available + lease.cpu)
    worker.memory_mb_available = min(
        worker.memory_mb_total, worker.memory_mb_available + lease.memory_mb,
    )
    worker.disk_gb_available += lease.disk_gb
    worker.active_runs = max(0, worker.active_runs - 1)
    lease.state = "released"


async def reconcile_terminal_lease(
    session: AsyncSession, worker: WorkerHost, lease_id: str,
    payload: LeaseReconciliationRequest,
) -> None:
    lease, item = await owned_lease(session, worker, lease_id)
    if item.id != str(payload.work_item_id) or item.version != payload.expected_version:
        raise HTTPException(409, "lease work identity or version mismatch")
    if item.status not in TERMINAL_WORK:
        raise HTTPException(409, "only terminal work can reconcile its lease")
    if lease.state not in {"active", "released"}:
        raise HTTPException(409, "lease cannot be reconciled")
    # Restart can lose the run token before or after its expiry. Expiry is not
    # proof of physical cleanup and is deliberately neither required nor renewed.
    job = await session.get(DeliveryJob, item.id, with_for_update=True)
    if job is not None and job.state not in {"completed", "failed"}:
        raise HTTPException(409, "delivery is not safely finished")
    recorded = await session.scalar(select(AuditRecord.id).where(
        AuditRecord.work_item_id == item.id, AuditRecord.target_id == lease.id,
        AuditRecord.action == "lease.reconciled",
    ).limit(1))
    if recorded is not None:
        if lease.state != "released":
            raise HTTPException(409, "lease contradicts its reconciliation history")
        return  # Lost ACK: never increment resources or repeat the audit/event.

    # The API cannot observe host disks. This is an authenticated declaration,
    # not independent physical proof. Worker must verify durable ownership,
    # domain absence, foreign references and file cleanup before calling here.
    state_before = lease.state
    if state_before == "active":
        return_lease_resources(worker, lease)
    session.add(AuditRecord(
        organization_id=item.organization_id, work_item_id=item.id, repository=item.repository,
        action="lease.reconciled", target_id=lease.id, actor_id=None,
        actor_subject=f"worker:{worker.id}", identity_provider="worker-credential",
        organization_role=None, repository_role=None, effective_role=None, required_role=None,
        request_id=current_request_id(), correlation_id=item.correlation_id,
        source_ip=None, transport="background", details={
            "worker_id": worker.id, "cleanup_confirmed": True,
            "lease_state_before": state_before, "lease_state_after": "released",
            "work_status": item.status.value, "work_version": item.version,
            "cpu": lease.cpu, "memory_mb": lease.memory_mb, "disk_gb": lease.disk_gb,
        },
    ))
    if state_before == "active":
        await emit_event(session, item.id, EventCreate(
            event_type="lease.released", source="control-plane",
            message="Worker resources reconciled after declared physical cleanup",
            payload={"worker_id": worker.id, "lease_id": lease.id, "reconciled": True},
        ))
