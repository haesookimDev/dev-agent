"""Lease authority is narrower than the control plane's state graph."""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditRecord, DeliveryJob, Role, WorkerHost, WorkItem, WorkStatus

# Explicit edges fail closed when the platform adds another transition.
WORKER_TRANSITIONS: dict[WorkStatus, set[WorkStatus]] = {
    WorkStatus.PROVISIONING: {WorkStatus.ANALYZING, WorkStatus.FAILED, WorkStatus.CANCELLED},
    WorkStatus.ANALYZING: {WorkStatus.IMPLEMENTING, WorkStatus.AWAITING_INPUT,
                          WorkStatus.BUDGET_EXHAUSTED, WorkStatus.FAILED, WorkStatus.CANCELLED},
    WorkStatus.IMPLEMENTING: {WorkStatus.VERIFYING, WorkStatus.AWAITING_INPUT,
                            WorkStatus.BUDGET_EXHAUSTED, WorkStatus.FAILED, WorkStatus.CANCELLED},
    WorkStatus.VERIFYING: {WorkStatus.IMPLEMENTING, WorkStatus.AWAITING_FEEDBACK,
                         WorkStatus.AWAITING_APPROVAL, WorkStatus.BUDGET_EXHAUSTED,
                         WorkStatus.FAILED, WorkStatus.CANCELLED},
    WorkStatus.AWAITING_FEEDBACK: {WorkStatus.AWAITING_APPROVAL, WorkStatus.CANCELLED},
    WorkStatus.AWAITING_APPROVAL: {WorkStatus.CANCELLED},
    WorkStatus.AWAITING_INPUT: {WorkStatus.CANCELLED},
    WorkStatus.BUDGET_EXHAUSTED: {WorkStatus.CANCELLED},
    # Progress here additionally requires an approved Mock run, never central delivery.
    WorkStatus.COMMITTING: {WorkStatus.PR_CREATED, WorkStatus.FAILED},
    WorkStatus.PR_CREATED: {WorkStatus.COMPLETED, WorkStatus.FAILED},
}


async def authorize_worker_transition(
    session: AsyncSession, item: WorkItem, target: WorkStatus, worker_id: str,
) -> None:
    def denied():
        return HTTPException(status.HTTP_403_FORBIDDEN, "worker cannot perform this transition")

    if (item.assigned_worker_id != worker_id
            or target not in WORKER_TRANSITIONS.get(item.status, set())):
        raise denied()
    if target not in {WorkStatus.PR_CREATED, WorkStatus.COMPLETED}:
        return
    worker = await session.get(WorkerHost, worker_id)
    if (worker is None or worker.labels.get("virtualization") != "mock"
            or await session.get(DeliveryJob, item.id) is not None):
        raise denied()
    source = await session.scalar(select(AuditRecord).where(
        AuditRecord.organization_id == item.organization_id,
        AuditRecord.work_item_id == item.id, AuditRecord.action == "approval.decided",
        AuditRecord.details["kind"].as_string() == "pull_request",
    ).order_by(AuditRecord.id.desc()).limit(1))
    if source is None or not isinstance(source.details, dict):
        raise denied()
    details = source.details
    version = details.get("work_version_after")
    # PR_CREATED is exactly one transition after the approved COMMITTING version.
    approved_version = item.version - int(item.status == WorkStatus.PR_CREATED)
    if (
        source.repository != item.repository or source.correlation_id != item.correlation_id
        or source.transport not in {"web", "slack"}
        or source.required_role != Role.APPROVER
        or source.effective_role not in {Role.APPROVER, Role.ADMINISTRATOR}
        or details.get("decision") != "approve" or details.get("delivery_queued") is not False
        or details.get("delivery_bundle_sha256") is not None
        or details.get("work_status_before") != "awaiting_approval"
        or details.get("work_status_after") != "committing"
        or type(version) is not int or version != approved_version
    ):
        raise denied()
