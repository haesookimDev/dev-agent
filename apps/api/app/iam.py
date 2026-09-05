"""Control-host policy provisioning. Never expose this module as an HTTP endpoint."""

import argparse
import asyncio
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Membership,
    Organization,
    Principal,
    Repository,
    RepositoryGrant,
    Role,
    SlackIdentity,
)

Subject = Annotated[str, Field(min_length=1, max_length=255)]


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemberPolicy(PolicyModel):
    subject: Subject
    role: Role


class RepositoryPolicy(PolicyModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=300)
    github_installation_id: int | None = Field(default=None, gt=0)
    grants: list[MemberPolicy] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.lower()


class SlackPolicy(PolicyModel):
    team_id: Subject
    user_id: Subject
    subject: Subject


class OrganizationPolicy(PolicyModel):
    organization_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    issuer: str = Field(min_length=1, max_length=1024)
    claim: Subject
    members: list[MemberPolicy]
    repositories: list[RepositoryPolicy] = Field(default_factory=list)
    slack_identities: list[SlackPolicy] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        if self.organization_id == "legacy":
            raise ValueError("legacy is reserved for quarantined historical work")
        if not self.issuer.startswith("https://"):
            raise ValueError("issuer must use https")
        subjects = {member.subject for member in self.members}
        if len(subjects) != len(self.members):
            raise ValueError("duplicate member")
        if not any(member.role == Role.ADMINISTRATOR for member in self.members):
            raise ValueError("at least one organization administrator is required")
        if len({repo.name for repo in self.repositories}) != len(self.repositories):
            raise ValueError("duplicate repository")
        for repo in self.repositories:
            if len({grant.subject for grant in repo.grants}) != len(repo.grants):
                raise ValueError("duplicate repository grant")
            if any(grant.subject not in subjects for grant in repo.grants):
                raise ValueError("repository grants require organization membership")
        if any(identity.subject not in subjects for identity in self.slack_identities):
            raise ValueError("Slack identities require organization membership")
        bindings = {(identity.team_id, identity.user_id) for identity in self.slack_identities}
        if len(bindings) != len(self.slack_identities):
            raise ValueError("duplicate Slack identity")
        return self


async def apply_policy(session: AsyncSession, policy: OrganizationPolicy) -> None:
    """Replace one organization's policy atomically in the caller's transaction."""
    organization = await session.get(Organization, policy.organization_id, with_for_update=True)
    if organization is None:
        organization = Organization(id=policy.organization_id, issuer=policy.issuer,
                                    claim=policy.claim)
        session.add(organization)
        await session.flush()
    elif (organization.issuer, organization.claim) != (policy.issuer, policy.claim):
        raise ValueError("an organization's identity binding cannot be reassigned")

    principal_ids = {}
    for member in policy.members:
        principal = await session.scalar(select(Principal).where(
            Principal.issuer == policy.issuer, Principal.subject == member.subject,
        ))
        if principal is None:
            principal = Principal(issuer=policy.issuer, subject=member.subject)
            session.add(principal)
            await session.flush()
        principal_ids[member.subject] = principal.id

    repository_names = select(Repository.name).where(
        Repository.organization_id == organization.id
    )
    await session.execute(delete(RepositoryGrant).where(
        RepositoryGrant.repository.in_(repository_names)
    ))
    await session.execute(delete(SlackIdentity).where(
        SlackIdentity.organization_id == organization.id
    ))
    await session.execute(delete(Membership).where(Membership.organization_id == organization.id))
    await session.execute(delete(Repository).where(Repository.organization_id == organization.id))
    for member in policy.members:
        session.add(Membership(organization_id=organization.id,
                               principal_id=principal_ids[member.subject], role=member.role))
    for repo in policy.repositories:
        existing = await session.get(Repository, repo.name)
        if existing is not None:
            raise ValueError("repository is already owned by another organization")
        session.add(Repository(name=repo.name, organization_id=organization.id,
                               github_installation_id=repo.github_installation_id))
    await session.flush()
    for repo in policy.repositories:
        for grant in repo.grants:
            session.add(RepositoryGrant(repository=repo.name,
                                        principal_id=principal_ids[grant.subject], role=grant.role))
    for identity in policy.slack_identities:
        existing = await session.get(SlackIdentity, (identity.team_id, identity.user_id))
        if existing is not None:
            raise ValueError("Slack identity is already bound to another organization")
        session.add(SlackIdentity(team_id=identity.team_id, user_id=identity.user_id,
                                  principal_id=principal_ids[identity.subject],
                                  organization_id=organization.id))
    await session.flush()


async def provision(policy: OrganizationPolicy) -> None:
    from .db import SessionLocal, engine, get_schema_readiness

    try:
        if not (await get_schema_readiness()).ready:
            raise RuntimeError("run database migrations before applying IAM policy")
        async with SessionLocal.begin() as session:
            await apply_policy(session, policy)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Replace one organization's IAM policy")
    parser.add_argument("policy", type=Path, help="complete organization policy JSON file")
    arguments = parser.parse_args()
    policy = OrganizationPolicy.model_validate_json(arguments.policy.read_text())
    asyncio.run(provision(policy))


if __name__ == "__main__":
    main()
