import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from test_authorization import authorized as authorized
from test_authorization import create_item, database, sign_in
from test_oidc import oidc_settings

from app.auth import hash_token
from app.config import Settings, get_settings
from app.main import app
from app.models import (
    AuditRecord,
    AuthSession,
    Membership,
    PreviewEndpoint,
    PreviewGrant,
    Principal,
    Repository,
    WorkerHost,
    WorkItem,
)


@pytest.fixture
async def preview(authorized):
    config = oidc_settings()
    config.preview_access_enabled = True
    config.preview_domain = "preview.example.net"
    config.gateway_secret = "synthetic-gateway-secret-32-characters"
    app.dependency_overrides[get_settings] = lambda: config
    item = await create_item(authorized)
    host = f"{item['id']}.{config.preview_domain}"
    async with database() as session:
        worker = WorkerHost(name="preview-test", cpu_total=2, cpu_available=2,
                            memory_mb_total=2048, memory_mb_available=2048,
                            disk_gb_available=20, labels={})
        session.add(worker)
        await session.flush()
        work = await session.get(WorkItem, item["id"])
        work.assigned_worker_id = worker.id
        session.add(PreviewEndpoint(work_item_id=work.id, hostname=host,
                                    target_url="http://10.0.0.2:3000",
                                    expires_at=datetime.now(UTC) + timedelta(minutes=20)))
        await session.commit()
    return authorized, config, item, host


async def issue(preview):
    client, _, item, _ = preview
    response = await client.post(f"/api/work-items/{item['id']}/preview-grants")
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def exchange(preview, gateway_headers, code=None, host=None, origin=None):
    client, config, _, actual_host = preview
    if code is None:
        code = (await issue(preview))["launch_code"]
    return await client.post("/internal/previews/exchange", params={"host": host or actual_host},
                             headers={**gateway_headers, "X-Kelpie-Preview-Code": code,
                                      "X-Kelpie-Launch-Origin": origin or config.dashboard_url})


async def access(preview, gateway_headers, token, host=None):
    client, _, _, actual_host = preview
    return await client.get("/internal/previews/authorize", params={"host": host or actual_host},
                            headers={**gateway_headers, "X-Kelpie-Preview-Token": token})


async def test_viewer_can_open_only_the_scoped_preview_without_leaking_credentials(
    preview, gateway_headers,
):
    client, _, item, host = preview
    await sign_in(client, "viewer")
    metadata = await client.get(f"/api/work-items/{item['id']}/preview-access")
    assert metadata.json()["available"] is True
    assert "target_url" not in metadata.json()
    launch = await issue(preview)
    assert launch["exchange_url"] == f"https://{host}/_kelpie/authorize"
    exchanged = await exchange(preview, gateway_headers, launch["launch_code"])
    assert exchanged.status_code == 200
    assert exchanged.headers["cache-control"] == "no-store"
    token = exchanged.json()["token"]
    response = await access(preview, gateway_headers, token)
    assert response.status_code == 200
    assert response.json() == {"target_url": "http://10.0.0.2:3000", "work_item_id": item["id"],
                               "read_only": True, "expires_at": exchanged.json()["expires_at"]}
    assert response.headers["cache-control"] == "no-store"
    async with database() as session:
        grant = (await session.scalars(select(PreviewGrant))).one()
        assert grant.launch_hash == hash_token(launch["launch_code"])
        assert grant.token_hash == hash_token(token)
        assert grant.exchanged_at is not None
        assert grant.expires_at - grant.created_at <= timedelta(minutes=5)
        audit = (await session.scalars(select(AuditRecord))).one()
        assert audit.action == "preview.granted"
        assert audit.target_id == grant.id
        assert audit.actor_subject == "viewer"
        assert audit.required_role == "viewer"
        assert audit.details == {"scope": "http_preview", "hostname": host}
        stored = str(grant.__dict__) + str(audit.__dict__)
        assert token not in stored
        assert launch["launch_code"] not in stored
        assert client.cookies.get("kelpie_session") not in stored


async def test_launch_is_single_use_and_cannot_be_used_as_access_token(preview, gateway_headers):
    launch = await issue(preview)
    code = launch["launch_code"]
    assert (await access(preview, gateway_headers, code)).status_code == 401
    assert (await exchange(preview, gateway_headers, code)).status_code == 200
    assert (await exchange(preview, gateway_headers, code)).status_code == 401


async def test_simultaneous_exchanges_issue_exactly_one_token(preview, gateway_headers):
    code = (await issue(preview))["launch_code"]
    results = await asyncio.gather(exchange(preview, gateway_headers, code),
                                   exchange(preview, gateway_headers, code))
    assert sorted(result.status_code for result in results) == [200, 401]
    token = next(result.json()["token"] for result in results if result.status_code == 200)
    assert (await access(preview, gateway_headers, token)).status_code == 200


@pytest.mark.parametrize("stage", ["launch", "access"])
@pytest.mark.parametrize("mutation", ["logout", "expiry", "issuer", "membership", "repository",
                                      "quarantine", "preview_expiry", "cidr", "target"])
async def test_current_identity_and_resource_policy_are_rechecked(
    preview, gateway_headers, stage, mutation,
):
    client, config, item, _ = preview
    launch = await issue(preview)
    credential = launch["launch_code"]
    if stage == "access":
        credential = (await exchange(preview, gateway_headers, credential)).json()["token"]
    async with database() as session:
        authenticated = await session.get(AuthSession, hash_token(client.cookies["kelpie_session"]))
        work = await session.get(WorkItem, item["id"])
        if mutation == "logout":
            await session.delete(authenticated)
        elif mutation == "expiry":
            authenticated.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif mutation == "issuer":
            config.oidc_issuer_url = "https://changed.example"
        elif mutation == "membership":
            principal = await session.scalar(select(Principal).where(
                Principal.subject == "admin", Principal.issuer == config.oidc_issuer_url,
            ))
            await session.execute(delete(Membership).where(Membership.principal_id == principal.id,
                                                          Membership.organization_id == "acme"))
        elif mutation == "repository":
            (await session.get(Repository, work.repository)).organization_id = "other"
        elif mutation == "quarantine":
            worker = await session.get(WorkerHost, work.assigned_worker_id)
            worker.quarantined_at = datetime.now(UTC)
        elif mutation == "preview_expiry":
            endpoint = await session.scalar(select(PreviewEndpoint))
            endpoint.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif mutation == "cidr":
            config.preview_allowed_cidrs = ["10.1.0.0/16"]
        elif mutation == "target":
            (await session.scalar(select(PreviewEndpoint))).target_url = "http://169.254.169.254"
        await session.commit()
    response = (await exchange(preview, gateway_headers, credential) if stage == "launch"
                else await access(preview, gateway_headers, credential))
    assert response.status_code in {401, 403, 404, 410}
    assert "target_url" not in response.json()
    assert credential not in response.text


async def test_wrong_host_and_origin_do_not_consume_launch(preview, gateway_headers):
    code = (await issue(preview))["launch_code"]
    assert (await exchange(preview, gateway_headers, code, host="other.preview.example.net")
            ).status_code == 401
    assert (await exchange(preview, gateway_headers, code, origin="https://evil.example")
            ).status_code == 403
    result = await exchange(preview, gateway_headers, code)
    assert result.status_code == 200
    assert (await access(preview, gateway_headers, result.json()["token"],
                         host="other.preview.example.net")).status_code == 401


@pytest.mark.parametrize("field", ["launch_expires_at", "expires_at"])
async def test_expired_launch_cannot_be_exchanged(preview, gateway_headers, field):
    code = (await issue(preview))["launch_code"]
    async with database() as session:
        grant = (await session.scalars(select(PreviewGrant))).one()
        setattr(grant, field, datetime.now(UTC) - timedelta(seconds=1))
        await session.commit()
    assert (await exchange(preview, gateway_headers, code)).status_code == 401


async def test_access_expires_even_if_parent_session_remains_live(preview, gateway_headers):
    result = await exchange(preview, gateway_headers)
    async with database() as session:
        (await session.scalar(select(PreviewGrant))).expires_at = datetime.now(UTC)
        await session.commit()
    assert (await access(preview, gateway_headers, result.json()["token"])).status_code == 401


@pytest.mark.parametrize("bound", ["session", "preview"])
async def test_grant_never_outlives_parent_session_or_preview(preview, bound):
    client, _, _, _ = preview
    expiry = datetime.now(UTC) + timedelta(seconds=20)
    async with database() as session:
        target = (await session.get(AuthSession, hash_token(client.cookies["kelpie_session"]))
                  if bound == "session" else await session.scalar(select(PreviewEndpoint)))
        target.expires_at = expiry
        await session.commit()
    await issue(preview)
    async with database() as session:
        grant = await session.scalar(select(PreviewGrant))
        assert grant.expires_at.replace(tzinfo=UTC) == expiry
        assert grant.launch_expires_at.replace(tzinfo=UTC) == expiry


async def test_cross_organization_or_unauthenticated_issuance_is_denied(preview):
    client, _, item, _ = preview
    await sign_in(client, organization="other")
    for suffix in ("preview-access", "preview-grants"):
        request = client.get if suffix == "preview-access" else client.post
        assert (await request(f"/api/work-items/{item['id']}/{suffix}")).status_code == 404
    client.cookies.clear()
    assert (await client.post(f"/api/work-items/{item['id']}/preview-grants",
                             headers={"X-Kelpie-User": "admin"})).status_code == 401
    async with database() as session:
        assert not list(await session.scalars(select(PreviewGrant)))
        assert not list(await session.scalars(select(AuditRecord)))


@pytest.mark.parametrize("enabled", [False, True])
async def test_development_or_disabled_configuration_cannot_issue_grants(preview, enabled):
    client, config, item, _ = preview
    config.preview_access_enabled = enabled
    if enabled:
        config.auth_mode = "development"
    # An existing OIDC session must never turn the development actor into a grant issuer.
    response = await client.post(f"/api/work-items/{item['id']}/preview-grants")
    assert response.status_code == 503
    async with database() as session:
        assert not list(await session.scalars(select(PreviewGrant)))


@pytest.mark.parametrize("value", ["", "kpl_short", "kpa_" + "x" * 43,
                                   "Bearer forged", "x" * 2000])
async def test_malformed_launch_does_not_reflect_input(preview, gateway_headers, value):
    result = await exchange(preview, gateway_headers, value)
    assert result.status_code == 401
    assert result.json() == {"detail": "invalid preview grant"}


async def test_gateway_service_credential_is_required(preview):
    code = (await issue(preview))["launch_code"]
    assert (await exchange(preview, {}, code)).status_code == 401
    assert (await access(preview, {}, "kpa_" + secrets.token_urlsafe(32))).status_code == 401


async def test_cross_origin_launch_cannot_create_grant(preview):
    client, _, item, _ = preview
    result = await client.post(f"/api/work-items/{item['id']}/preview-grants",
                               headers={"Origin": "https://evil.example"})
    assert result.status_code == 403


@pytest.mark.parametrize("domain", ["preview.example.com", "dashboard.example.com", "example.com",
                                    "preview.example.net:8080", "bad/preview.example.net"])
async def test_untrusted_domain_cannot_overlap_or_inject_a_trusted_origin(preview, domain):
    client, config, item, _ = preview
    config.preview_domain = domain
    config.dashboard_url = "https://dashboard.example.com"
    config.oidc_redirect_uri = "https://control.example.com/auth/callback"
    client.headers["Origin"] = config.dashboard_url
    async with database() as session:
        (await session.scalar(select(PreviewEndpoint))).hostname = f"{item['id']}.{domain}"
        await session.commit()
    assert (await client.post(f"/api/work-items/{item['id']}/preview-grants")).status_code == 503


async def test_preview_unavailable_is_not_exposed_as_a_work_target(preview):
    client, _, item, _ = preview
    async with database() as session:
        await session.execute(delete(PreviewEndpoint))
        await session.commit()
    response = await client.get(f"/api/work-items/{item['id']}/preview-access")
    assert response.json() == {"available": False, "reason": "unavailable"}
    assert (await client.post(f"/api/work-items/{item['id']}/preview-grants")).status_code == 404


@pytest.mark.parametrize("target", [
    "http://[", "http://10.0.0.2:bad", "https://10.0.0.2", "http://localhost",
    "http://user@10.0.0.2", "http://10.0.0.2:0", "http://10.0.0.2/private",
    "http://10.0.0.2?route=other", "http://10.0.0.2#other",
])
async def test_resolution_rejects_unsafe_or_malformed_stored_targets(
    preview, gateway_headers, target,
):
    token = (await exchange(preview, gateway_headers)).json()["token"]
    async with database() as session:
        (await session.scalar(select(PreviewEndpoint))).target_url = target
        await session.commit()
    response = await access(preview, gateway_headers, token)
    assert response.status_code == 410
    assert response.json() == {"detail": "preview target is not allowed"}


def test_preview_access_defaults_to_disabled():
    assert Settings(_env_file=None).preview_access_enabled is False
