import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.iam import OrganizationPolicy, apply_policy
from app.models import Base, Membership, Principal, Repository, RepositoryGrant, SlackIdentity


def policy_data(organization="acme") -> dict:
    return {
        "organization_id": organization, "issuer": "https://identity.example",
        "claim": organization,
        "members": [{"subject": "admin", "role": "administrator"},
                    {"subject": "user-123", "role": "viewer"}],
        "repositories": [{"name": f"{organization}/service", "github_installation_id": 12,
                          "grants": [{"subject": "user-123", "role": "approver"}]}],
        "slack_identities": [{"team_id": f"T-{organization}", "user_id": "U1",
                              "subject": "user-123"}],
    }


@pytest.fixture
async def policy_sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'policy.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_policy_replacement_is_idempotent_and_revokes_omitted_grants(policy_sessions):
    data = policy_data()
    for _ in range(2):
        async with policy_sessions.begin() as session:
            await apply_policy(session, OrganizationPolicy.model_validate(data))
    async with policy_sessions() as session:
        assert len((await session.scalars(select(Principal))).all()) == 2
        assert len((await session.scalars(select(RepositoryGrant))).all()) == 1
    data["members"].pop()
    data["repositories"][0]["grants"] = []
    data["slack_identities"] = []
    async with policy_sessions.begin() as session:
        await apply_policy(session, OrganizationPolicy.model_validate(data))
    async with policy_sessions() as session:
        assert len((await session.scalars(select(Membership))).all()) == 1
        assert not (await session.scalars(select(RepositoryGrant))).all()
        assert not (await session.scalars(select(SlackIdentity))).all()


async def test_policy_cannot_claim_another_organizations_repository(policy_sessions):
    async with policy_sessions.begin() as session:
        await apply_policy(session, OrganizationPolicy.model_validate(policy_data()))
    data = policy_data("other")
    data["repositories"][0]["name"] = "ACME/Service"
    with pytest.raises(ValueError, match="another organization"):
        async with policy_sessions.begin() as session:
            await apply_policy(session, OrganizationPolicy.model_validate(data))
    async with policy_sessions() as session:
        assert (await session.get(Repository, "acme/service")).organization_id == "acme"
        assert len((await session.scalars(select(Membership))).all()) == 2


@pytest.mark.parametrize("change", [
    {"organization_id": "legacy"},
    {"members": [{"subject": "user-123", "role": "viewer"}]},
    {"repositories": [{"name": "acme/service", "grants": [
        {"subject": "outsider", "role": "administrator"}
    ]}]},
])
def test_invalid_policies_are_rejected(change):
    with pytest.raises(ValidationError):
        OrganizationPolicy.model_validate({**policy_data(), **change})
