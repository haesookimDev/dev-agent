import ipaddress
from dataclasses import dataclass
from datetime import UTC
from typing import Literal

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import Actor
from .authorization import RoleDecision
from .correlation import current_request_id
from .models import Approval, AuditRecord, ConsoleLease, DeliveryBundle, Feedback, WorkItem


def request_source_ip(request: Request) -> str | None:
    """Use the ASGI peer; never interpret user-supplied forwarding headers here."""
    if request.client is None:
        return None
    try:
        return str(ipaddress.ip_address(request.client.host))
    except ValueError:
        return None


def record_feedback_audit(
    session: AsyncSession, request: Request, item: WorkItem, actor: Actor,
    decision: RoleDecision, feedback: Feedback, *, transport: Literal["web", "slack"],
) -> None:
    if feedback.id is None:
        raise ValueError("feedback must be flushed before recording its audit identity")
    _record_actor_audit(session, request, item, actor, decision, action="feedback.created",
                        target_id=str(feedback.id), transport=transport, details={})


@dataclass(frozen=True)
class ConsoleOwnership:
    holder_type: str
    holder: str
    version: int

    @classmethod
    def capture(cls, lease: ConsoleLease) -> "ConsoleOwnership":
        return cls(lease.holder_type, lease.holder, lease.version)


def record_cancellation_audit(
    session: AsyncSession, request: Request, item: WorkItem, actor: Actor,
    decision: RoleDecision, *, version_before: int,
) -> None:
    _record_actor_audit(
        session, request, item, actor, decision, action="work.cancelled",
        target_id=item.id, transport="web", details={
            "scope": "unassigned_queue", "work_status_before": "queued",
            "work_status_after": item.status.value, "work_version_before": version_before,
            "work_version_after": item.version,
        },
    )


def record_console_audit(
    session: AsyncSession, request: Request, item: WorkItem, actor: Actor,
    decision: RoleDecision, lease: ConsoleLease, before: ConsoleOwnership,
    *, action: Literal["acquire", "release"],
) -> None:
    expiry = lease.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    _record_actor_audit(
        session, request, item, actor, decision, action="console.transferred",
        target_id=item.id, transport="web", details={
            "action": action, "holder_type_before": before.holder_type,
            "holder_before": before.holder, "version_before": before.version,
            "holder_type_after": lease.holder_type, "holder_after": lease.holder,
            "version_after": lease.version, "expires_at": expiry.isoformat(),
        },
    )


@dataclass(frozen=True)
class ApprovalState:
    budget_minutes: int
    status: str
    version: int

    @classmethod
    def capture(cls, item: WorkItem) -> "ApprovalState":
        return cls(item.budget_minutes, item.status.value, item.version)


async def record_approval_audit(
    session: AsyncSession, request: Request, item: WorkItem, actor: Actor,
    decision: RoleDecision, approval: Approval, before: ApprovalState,
    *, transport: Literal["web", "slack"], delivery_queued: bool,
) -> None:
    if approval.id is None:
        raise ValueError("approval must be flushed before recording its audit identity")
    bundle = await session.get(DeliveryBundle, item.id) if delivery_queued else None
    if delivery_queued and bundle is None:
        raise ValueError("queued delivery requires its approved bundle audit identity")
    _record_actor_audit(
        session, request, item, actor, decision, action="approval.decided",
        target_id=str(approval.id), transport=transport, details={
            "kind": approval.kind, "decision": approval.decision,
            "budget_minutes_before": before.budget_minutes,
            "budget_minutes_after": item.budget_minutes,
            "work_status_before": before.status, "work_status_after": item.status.value,
            "work_version_before": before.version, "work_version_after": item.version,
            "delivery_queued": delivery_queued,
            "delivery_bundle_sha256": bundle.sha256 if bundle else None,
        },
    )


def _record_actor_audit(
    session: AsyncSession, request: Request, item: WorkItem, actor: Actor,
    decision: RoleDecision, *, action: str, target_id: str,
    transport: Literal["web", "slack"], details: dict,
) -> None:
    session.add(AuditRecord(
        organization_id=item.organization_id, work_item_id=item.id, repository=item.repository,
        action=action, target_id=target_id, actor_id=actor.principal_id,
        actor_subject=actor.subject, identity_provider=actor.identity_provider,
        organization_role=decision.organization_role, repository_role=decision.repository_role,
        effective_role=decision.effective_role, required_role=decision.required_role,
        request_id=current_request_id(), correlation_id=item.correlation_id,
        source_ip=request_source_ip(request), transport=transport, details=details,
    ))
