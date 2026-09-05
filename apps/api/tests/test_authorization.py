import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from test_iam import policy_data
from test_oidc import oidc_settings

from app.auth import hash_token
from app.config import get_settings
from app.db import get_session
from app.iam import OrganizationPolicy, apply_policy
from app.main import app
from app.models import AuthSession, Membership, Principal


@asynccontextmanager
async def database():
    async for session in app.dependency_overrides[get_session]():
        yield session


async def sign_in(client, subject="admin", organization="acme", issuer="https://identity.example"):
    token = secrets.token_urlsafe(32)
    async with database() as session:
        session.add(AuthSession(token_hash=hash_token(token), subject=subject,
                                identity_provider=issuer, organization=organization,
                                expires_at=datetime.now(UTC) + timedelta(hours=1)))
        await session.commit()
    client.cookies.set("kelpie_session", token)


@pytest.fixture
async def authorized(client):
    app.dependency_overrides[get_settings] = oidc_settings
    client.headers["Origin"] = "https://dashboard.example"
    async with database() as session:
        for org in ("acme", "other"):
            data = policy_data(org)
            data["members"] += [{"subject": "viewer", "role": "viewer"},
                                {"subject": "operator", "role": "operator"},
                                {"subject": "approver", "role": "approver"}]
            data["repositories"] += [{"name": f"{org}/second"}]
            await apply_policy(session, OrganizationPolicy.model_validate(data))
        await session.commit()
    await sign_in(client)
    return client


async def test_membership_is_required_and_rechecked_for_existing_session(authorized):
    client = authorized
    await sign_in(client, "viewer")
    response = await client.get("/auth/session")
    assert response.status_code == 200
    assert response.json()["role"] == "viewer"
    async with database() as session:
        principal = await session.scalar(select(Principal).where(Principal.subject == "viewer"))
        await session.execute(delete(Membership).where(
            Membership.organization_id == "acme", Membership.principal_id == principal.id,
        ))
        await session.commit()
    assert (await client.get("/auth/session")).status_code == 403


@pytest.mark.parametrize("identity", [
    {"subject": "unregistered"}, {"organization": "unregistered"},
    {"issuer": "https://another-issuer.example"},
])
async def test_unregistered_identity_is_denied(authorized, identity):
    await sign_in(authorized, **identity)
    assert (await authorized.get("/auth/session")).status_code in {401, 403}


async def test_cross_origin_cookie_mutation_is_denied(authorized):
    response = await authorized.post("/api/work-items", headers={"Origin": "https://evil.example"},
                                     json={"repository": "acme/service", "title": "test work",
                                           "requirement": "test requirement"})
    assert response.status_code == 403
