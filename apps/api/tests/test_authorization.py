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
from app.models import (
    Approval,
    Artifact,
    AuthSession,
    ConsoleLease,
    Feedback,
    Membership,
    Organization,
    Principal,
    Repository,
    RepositoryGrant,
    WorkItem,
)


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
    artifact_root = app.dependency_overrides[get_settings]().artifact_root
    app.dependency_overrides[get_settings] = lambda: oidc_settings().model_copy(
        update={"artifact_root": artifact_root},
    )
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


async def create_item(client, repository="acme/service"):
    response = await client.post("/api/work-items", json={
        "repository": repository, "title": "Authorization test", "requirement": "Test access",
    })
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("subject,can_operate,can_approve", [
    ("viewer", False, False), ("operator", True, False), ("approver", True, True),
    ("admin", True, True), ("user-123", True, True),
])
async def test_role_matrix_for_all_mutations(authorized, subject, can_operate, can_approve):
    client = authorized
    item = await create_item(client)
    work_id = item["id"]
    async with database() as session:
        session.add(ConsoleLease(work_item_id=work_id, holder="agent", holder_type="agent",
                                  expires_at=datetime.now(UTC) + timedelta(hours=1)))
        await session.commit()
    await sign_in(client, subject)
    prefix = f"/api/work-items/{work_id}"
    created = await client.post("/api/work-items", json={
        "repository": "ACME/Service", "title": "Another item", "requirement": "Test creation",
    })
    assert created.status_code == (201 if can_operate else 403)
    feedback = await client.post(f"{prefix}/feedback", json={"message": "feedback"})
    assert feedback.status_code == (200 if can_operate else 403)
    console = await client.post(f"{prefix}/console-lease", json={"action": "acquire"})
    assert console.status_code == (200 if can_operate else 403)
    approval = await client.post(f"{prefix}/approvals", json={
        "kind": "console", "decision": "approve",
    })
    assert approval.status_code == (200 if can_approve else 403)
    async with database() as session:
        assert bool(await session.scalar(select(Feedback).where(
            Feedback.work_item_id == work_id
        ))) == can_operate
        assert bool(await session.scalar(select(Approval).where(
            Approval.work_item_id == work_id
        ))) == can_approve


@pytest.mark.parametrize("suffix", ["", "/event-log", "/events", "/artifacts",
                                     "/artifacts/artifact-1", "/delivery-bundle"])
async def test_cross_organization_reads_are_hidden(authorized, suffix):
    item = await create_item(authorized)
    async with database() as session:
        session.add(Artifact(id="artifact-1", work_item_id=item["id"], kind="test", name="test.txt",
                             object_key="test.txt", content_type="text/plain", size_bytes=1))
        await session.commit()
    await sign_in(authorized, organization="other")
    response = await authorized.get(f"/api/work-items/{item['id']}{suffix}")
    assert response.status_code == 404


@pytest.mark.parametrize("suffix,payload", [
    ("feedback", {"message": "cross-org feedback"}),
    ("console-lease", {"action": "acquire"}),
    ("approvals", {"kind": "console", "decision": "approve"}),
])
async def test_cross_organization_mutations_are_hidden(authorized, suffix, payload):
    item = await create_item(authorized)
    await sign_in(authorized, organization="other")
    response = await authorized.post(f"/api/work-items/{item['id']}/{suffix}", json=payload)
    assert response.status_code == 404


async def test_listing_filters_organization_before_limit(authorized):
    own = await create_item(authorized)
    await sign_in(authorized, organization="other")
    await create_item(authorized, "other/service")
    await sign_in(authorized, "viewer")
    response = await authorized.get("/api/work-items?limit=1")
    assert [item["id"] for item in response.json()] == [own["id"]]


@pytest.mark.parametrize("repository", ["unknown/service", "other/service"])
async def test_unregistered_or_other_repository_cannot_create_work(authorized, repository):
    response = await authorized.post("/api/work-items", json={
        "repository": repository, "title": "Unauthorized", "requirement": "Test creation",
        "organization_id": "other",
    })
    assert response.status_code == 404


async def test_grant_is_repository_scoped_and_revocation_is_immediate(authorized):
    first = await create_item(authorized)
    second = await create_item(authorized, "acme/second")
    await sign_in(authorized, "user-123")
    for item, expected in ((first, 200), (second, 403)):
        response = await authorized.post(f"/api/work-items/{item['id']}/approvals", json={
            "kind": "console", "decision": "approve",
        })
        assert response.status_code == expected
    async with database() as session:
        await session.execute(delete(RepositoryGrant).where(
            RepositoryGrant.repository == "acme/service"
        ))
        await session.commit()
    response = await authorized.post(f"/api/work-items/{first['id']}/approvals", json={
        "kind": "console", "decision": "approve",
    })
    assert response.status_code == 403


@pytest.mark.parametrize("revoke", ["membership", "session", "repository"])
async def test_event_stream_rechecks_authorization(authorized, monkeypatch, revoke):
    import asyncio

    from starlette.requests import Request

    from app.auth import current_actor
    from app.main import stream_events

    item = await create_item(authorized)
    await sign_in(authorized, "viewer")
    request = Request({"type": "http", "method": "GET", "headers": [
        (b"cookie", f"kelpie_session={authorized.cookies.get('kelpie_session')}".encode())
    ]})
    monkeypatch.setattr("app.main.SessionLocal", database)
    async with database() as session:
        actor = await current_actor(request, oidc_settings(), session)
        response = await stream_events(request, item["id"], session, actor, oidc_settings(),
                                       after=0, last_event_id=None)
    first = await anext(response.body_iterator)
    assert "work.created" in first
    async with database() as session:
        if revoke == "membership":
            await session.execute(delete(Membership).where(
                Membership.principal_id == actor.principal_id,
                Membership.organization_id == "acme",
            ))
        elif revoke == "session":
            authenticated = await session.get(AuthSession, hash_token(
                authorized.cookies.get("kelpie_session")
            ))
            authenticated.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        else:
            await session.execute(delete(RepositoryGrant).where(
                RepositoryGrant.repository == "acme/service"
            ))
            await session.execute(delete(Repository).where(Repository.name == "acme/service"))
        await session.commit()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(response.body_iterator), timeout=3)


async def test_historical_work_is_not_adopted_by_matching_repository_name(authorized):
    item = await create_item(authorized)
    async with database() as session:
        session.add(Organization(id="legacy"))
        await session.flush()
        historical = await session.get(WorkItem, item["id"])
        historical.organization_id = "legacy"
        await session.commit()
    assert (await authorized.get(f"/api/work-items/{item['id']}")).status_code == 404
    assert (await authorized.get("/api/work-items")).json() == []


async def test_header_cannot_elevate_viewer_role(authorized):
    item = await create_item(authorized)
    await sign_in(authorized, "viewer")
    response = await authorized.post(f"/api/work-items/{item['id']}/approvals", headers={
        "X-Kelpie-User": "admin", "X-Kelpie-Role": "administrator",
    }, json={"kind": "console", "decision": "approve"})
    assert response.status_code == 403
