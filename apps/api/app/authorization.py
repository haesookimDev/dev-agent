from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import Actor, actor_from_identity
from .models import (
    Organization,
    Principal,
    Repository,
    RepositoryGrant,
    Role,
    SlackIdentity,
    WorkItem,
)

ROLE_ORDER = {role: rank for rank, role in enumerate(Role)}


@dataclass(frozen=True)
class RoleDecision:
    organization_role: Role
    repository_role: Role | None
    effective_role: Role
    required_role: Role


async def authorize_repository(
    session: AsyncSession, actor: Actor, name: str, required: Role = Role.VIEWER,
) -> Repository:
    repository, _ = await authorize_repository_with_decision(session, actor, name, required)
    return repository


async def authorize_repository_with_decision(
    session: AsyncSession, actor: Actor, name: str, required: Role = Role.VIEWER,
) -> tuple[Repository, RoleDecision]:
    repository = await session.get(Repository, name.lower())
    if repository is None or repository.organization_id != actor.organization:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")
    effective = ROLE_ORDER.get(actor.role, -1)
    if effective < 0:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid organization role")
    repository_role = None
    if actor.principal_id:
        grant = await session.get(RepositoryGrant, (repository.name, actor.principal_id))
        if grant is not None:
            repository_role = grant.role
            effective = max(effective, ROLE_ORDER[grant.role])
    if effective < ROLE_ORDER[required]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{required.value} role required")
    return repository, RoleDecision(
        organization_role=Role(actor.role), repository_role=repository_role,
        effective_role=list(Role)[effective], required_role=required,
    )


async def authorized_work(
    session: AsyncSession, actor: Actor, work_item_id: str,
    required: Role = Role.VIEWER, *, lock: bool = False,
) -> WorkItem:
    item, _ = await authorized_work_with_decision(
        session, actor, work_item_id, required, lock=lock,
    )
    return item


async def authorized_work_with_decision(
    session: AsyncSession, actor: Actor, work_item_id: str,
    required: Role = Role.VIEWER, *, lock: bool = False,
) -> tuple[WorkItem, RoleDecision]:
    statement = select(WorkItem).where(WorkItem.id == work_item_id,
                                       WorkItem.organization_id == actor.organization)
    if lock:
        statement = statement.with_for_update()
    item = await session.scalar(statement)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "work item not found")
    _, decision = await authorize_repository_with_decision(
        session, actor, item.repository, required,
    )
    return item, decision


async def development_repository(session: AsyncSession, organization_id: str, name: str) -> None:
    """Only callers in explicit development mode may auto-register local repositories."""
    if organization_id == "legacy":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "legacy organization is quarantined")
    organization = await session.get(Organization, organization_id)
    if organization is None:
        session.add(Organization(id=organization_id))
        await session.flush()
    elif organization.issuer is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "development organization is reserved")
    if await session.get(Repository, name.lower()) is None:
        session.add(Repository(name=name.lower(), organization_id=organization_id))
        await session.flush()


async def slack_actor(session: AsyncSession, team_id: str, user_id: str) -> Actor:
    identity = await session.get(SlackIdentity, (team_id, user_id))
    if identity is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Slack identity is not linked")
    principal = await session.get(Principal, identity.principal_id)
    organization = await session.get(Organization, identity.organization_id)
    if principal is None or organization is None or not organization.claim:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Slack identity is not linked")
    return await actor_from_identity(
        session, principal.issuer, principal.subject, organization.claim
    )
