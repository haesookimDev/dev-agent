import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import hash_token
from .correlation import current_correlation_id
from .models import (
    AgentEvent,
    ResourceLease,
    WorkerHost,
    WorkerState,
    WorkItem,
    WorkSource,
    WorkStatus,
    utcnow,
)
from .observability import observe_claim, observe_transition
from .schemas import ClaimRequest, EventCreate, WorkItemCreate
from .state_machine import InvalidTransition, ensure_transition


async def emit_event(
    session: AsyncSession,
    work_item_id: str,
    event: EventCreate,
) -> AgentEvent:
    item = await session.get(WorkItem, work_item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "work item not found")
    record = AgentEvent(
        work_item_id=work_item_id,
        correlation_id=item.correlation_id,
        **event.model_dump(),
    )
    session.add(record)
    await session.flush()
    return record


async def create_work_item(
    session: AsyncSession,
    payload: WorkItemCreate,
    *,
    source: WorkSource,
    requested_by: str,
    organization_id: str,
    source_external_id: str | None = None,
    github_installation_id: int | None = None,
    github_issue_number: int | None = None,
    correlation_id: str | None = None,
) -> WorkItem:
    item = WorkItem(
        **payload.model_dump(),
        organization_id=organization_id,
        source=source,
        requested_by=requested_by,
        source_external_id=source_external_id,
        github_installation_id=github_installation_id,
        github_issue_number=github_issue_number,
        correlation_id=correlation_id or current_correlation_id(),
    )
    session.add(item)
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "work item already exists") from error
    await emit_event(
        session,
        item.id,
        EventCreate(event_type="work.created", message=f"Work item created from {source.value}"),
    )
    return item


async def transition_work_item(
    session: AsyncSession,
    item: WorkItem,
    target: WorkStatus,
    *,
    expected_version: int,
    actor: str,
    message: str = "",
    payload: dict | None = None,
) -> WorkItem:
    if item.version != expected_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"version mismatch: current version is {item.version}",
        )
    try:
        ensure_transition(item.status, target)
    except InvalidTransition as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    previous = item.status
    transitioned_at = utcnow()
    previous_updated_at = item.updated_at
    if previous_updated_at.tzinfo is None:
        previous_updated_at = previous_updated_at.replace(tzinfo=UTC)
    item.status = target
    item.version += 1
    item.updated_at = transitioned_at
    await emit_event(
        session,
        item.id,
        EventCreate(
            event_type="work.transitioned",
            source=actor,
            message=message or f"{previous.value} → {target.value}",
            payload={"from": previous.value, "to": target.value, **(payload or {})},
        ),
    )
    await session.flush()
    observe_transition(
        previous.value,
        target.value,
        (transitioned_at - previous_updated_at).total_seconds(),
    )
    return item


async def claim_next_work(
    session: AsyncSession,
    worker: WorkerHost,
    request: ClaimRequest,
    lease_seconds: int,
) -> tuple[WorkItem, str, ResourceLease] | None:
    if worker.state != WorkerState.ONLINE or worker.quarantined_at is not None:
        observe_claim("unavailable_worker")
        return None
    if (
        worker.cpu_available < request.cpu
        or worker.memory_mb_available < request.memory_mb
        or worker.disk_gb_available < request.disk_gb
    ):
        observe_claim("insufficient_resources")
        return None
    statement = (
        select(WorkItem)
        .where(WorkItem.status == WorkStatus.QUEUED)
        .order_by(WorkItem.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    item = (await session.execute(statement)).scalar_one_or_none()
    if item is None:
        observe_claim("empty")
        return None
    created_at = item.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    observe_claim("claimed", queued_seconds=(datetime.now(UTC) - created_at).total_seconds())
    token = secrets.token_urlsafe(32)
    expiry = datetime.now(UTC) + timedelta(seconds=lease_seconds)
    item.assigned_worker_id = worker.id
    await transition_work_item(
        session,
        item,
        WorkStatus.PROVISIONING,
        expected_version=item.version,
        actor=f"worker:{worker.name}",
    )
    lease = ResourceLease(
        work_item_id=item.id,
        worker_id=worker.id,
        token_hash=hash_token(token),
        expires_at=expiry,
        cpu=request.cpu,
        memory_mb=request.memory_mb,
        disk_gb=request.disk_gb,
    )
    session.add(lease)
    worker.cpu_available -= request.cpu
    worker.memory_mb_available -= request.memory_mb
    worker.disk_gb_available -= request.disk_gb
    worker.active_runs += 1
    await session.flush()
    return item, token, lease


async def validate_lease(
    session: AsyncSession,
    work_item_id: str,
    token: str | None,
    lease_seconds: int,
) -> ResourceLease:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing lease token")
    statement = select(ResourceLease).where(
        ResourceLease.work_item_id == work_item_id,
        ResourceLease.token_hash == hash_token(token),
        ResourceLease.state == "active",
    )
    lease = (await session.execute(statement)).scalar_one_or_none()
    if lease is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid lease token")
    now = datetime.now(UTC)
    expires_at = lease.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "expired lease token")
    lease.expires_at = now + timedelta(seconds=lease_seconds)
    return lease
