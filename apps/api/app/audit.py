import ipaddress
from typing import Literal

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import Actor
from .authorization import RoleDecision
from .correlation import current_request_id
from .models import AuditRecord, Feedback, WorkItem


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
    session.add(AuditRecord(
        organization_id=item.organization_id, work_item_id=item.id, repository=item.repository,
        action="feedback.created", target_id=str(feedback.id), actor_id=actor.principal_id,
        actor_subject=actor.subject, identity_provider=actor.identity_provider,
        organization_role=decision.organization_role, repository_role=decision.repository_role,
        effective_role=decision.effective_role, required_role=decision.required_role,
        request_id=current_request_id(), correlation_id=item.correlation_id,
        source_ip=request_source_ip(request), transport=transport,
    ))
