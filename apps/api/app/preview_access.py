"""OIDC-bound HTTP preview access. This does not grant desktop/console input."""

import ipaddress
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .audit import record_preview_audit
from .auth import Actor, actor_from_identity, current_actor, hash_token, require_gateway
from .authorization import authorized_work, authorized_work_with_decision
from .config import Settings, get_settings
from .db import get_session
from .models import AuthSession, PreviewEndpoint, PreviewGrant, WorkerHost, WorkItem

router = APIRouter()
SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ActorDep = Annotated[Actor, Depends(current_actor)]
GatewayDep = Annotated[None, Depends(require_gateway)]
HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST = re.compile(HOST_LABEL + r"(?:\." + HOST_LABEL + r")+")
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def reject(code: int, message: str) -> HTTPException:
    return HTTPException(code, message, headers=NO_STORE)


def utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def require_oidc(config: Settings) -> None:
    if config.auth_mode != "oidc" or not config.preview_access_enabled:
        raise reject(503, "OIDC preview access is not configured")


def expected_hostname(config: Settings, work_id: str) -> str:
    host = f"{work_id}.{config.preview_domain}".lower()
    if len(host) > 253 or not HOST.fullmatch(host):
        raise reject(503, "preview domain is invalid")
    # Untrusted previews must never share the dashboard or authentication origin.
    for trusted_url in (config.dashboard_url, config.oidc_redirect_uri):
        trusted = urlsplit(trusted_url).hostname or ""
        # Conservative site separation without a stale bundled public-suffix list.
        # Multi-label public suffixes may require a different preview domain.
        shared_suffix = ".".join(trusted.split(".")[-2:]) == ".".join(host.split(".")[-2:])
        if (shared_suffix or trusted == host
                or trusted.endswith("." + config.preview_domain.lower())):
            raise reject(503, "preview domain overlaps a trusted origin")
    return host


async def live_session(session: AsyncSession, config: Settings, token_hash: str) -> AuthSession:
    authenticated = await session.get(AuthSession, token_hash, populate_existing=True)
    if (authenticated is None or utc(authenticated.expires_at) <= datetime.now(UTC)
            or authenticated.identity_provider != config.oidc_issuer_url):
        raise reject(401, "preview authentication expired")
    return authenticated


async def live_preview(
    session: AsyncSession, config: Settings, work: WorkItem,
) -> PreviewEndpoint:
    # Same lock order as quarantine and worker registration: Worker -> Work/Preview.
    worker = (await session.get(WorkerHost, work.assigned_worker_id, with_for_update=True,
                               populate_existing=True) if work.assigned_worker_id else None)
    if worker is None or worker.quarantined_at is not None:
        raise reject(410, "preview unavailable")
    work = await session.get(WorkItem, work.id, with_for_update=True, populate_existing=True)
    if work is None or work.assigned_worker_id != worker.id:
        raise reject(410, "preview worker changed")
    endpoint = await session.scalar(select(PreviewEndpoint).where(
        PreviewEndpoint.work_item_id == work.id,
    ).execution_options(populate_existing=True))
    if endpoint is None:
        raise reject(404, "preview not registered")
    if utc(endpoint.expires_at) <= datetime.now(UTC):
        raise reject(410, "preview expired")
    if endpoint.hostname != expected_hostname(config, work.id):
        raise reject(410, "preview hostname changed")
    # Recheck the current CIDR policy on every resolution, not only registration.
    try:
        target = urlsplit(endpoint.target_url)
        address = ipaddress.ip_address(target.hostname or "")
        allowed = any(address in ipaddress.ip_network(cidr)
                      for cidr in config.preview_allowed_cidrs)
        port = target.port
    except ValueError:
        raise reject(410, "preview target is not allowed") from None
    if (not allowed or target.scheme != "http" or target.username is not None
            or target.password is not None or target.query or target.fragment
            or target.path not in {"", "/"} or port == 0):
        raise reject(410, "preview target is not allowed")
    return endpoint


@router.get("/api/work-items/{work_item_id}/preview-access")
async def preview_access(
    work_item_id: str, session: SessionDep, config: SettingsDep, actor: ActorDep,
    response: Response,
) -> dict:
    response.headers.update(NO_STORE)
    work = await authorized_work(session, actor, work_item_id)
    if config.auth_mode != "oidc" or not config.preview_access_enabled:
        return {"available": False, "reason": "not_configured"}
    try:
        endpoint = await live_preview(session, config, work)
    except HTTPException as error:
        if error.status_code not in {404, 410}:
            raise
        return {"available": False, "reason": "unavailable"}
    return {"available": True, "expires_at": utc(endpoint.expires_at)}


@router.post("/api/work-items/{work_item_id}/preview-grants", status_code=201)
async def issue_preview_grant(
    work_item_id: str, request: Request, response: Response,
    session: SessionDep, config: SettingsDep, actor: ActorDep,
) -> dict:
    require_oidc(config)
    work, decision = await authorized_work_with_decision(session, actor, work_item_id)
    endpoint = await live_preview(session, config, work)
    authenticated = await live_session(
        session, config, hash_token(request.cookies.get(config.oidc_session_cookie_name, "")),
    )
    now = datetime.now(UTC)
    expiry = min(now + timedelta(minutes=5), utc(authenticated.expires_at),
                 utc(endpoint.expires_at))
    code = "kpl_" + secrets.token_urlsafe(32)
    grant = PreviewGrant(
        work_item_id=work.id, auth_session_hash=authenticated.token_hash,
        hostname=endpoint.hostname, launch_hash=hash_token(code),
        launch_expires_at=min(now + timedelta(seconds=30), expiry), expires_at=expiry,
    )
    session.add(grant)
    await session.flush()
    record_preview_audit(session, request, work, actor, decision, grant)
    await session.commit()
    response.headers.update(NO_STORE)
    return {"launch_code": code, "exchange_url": f"https://{grant.hostname}/_kelpie/authorize",
            "expires_at": utc(grant.launch_expires_at)}


async def validate_grant(
    session: AsyncSession, config: Settings, grant: PreviewGrant | None, host: str,
) -> PreviewEndpoint:
    if (grant is None or grant.hostname != host or utc(grant.expires_at) <= datetime.now(UTC)):
        raise reject(401, "invalid preview grant")
    work = await session.get(WorkItem, grant.work_item_id)
    if work is None:
        raise reject(410, "preview unavailable")
    endpoint = await live_preview(session, config, work)
    # Worker lock acquisition may wait; recheck identity and expiry afterward.
    if utc(grant.expires_at) <= datetime.now(UTC):
        raise reject(401, "invalid preview grant")
    authenticated = await live_session(session, config, grant.auth_session_hash)
    actor = await actor_from_identity(session, authenticated.identity_provider,
                                     authenticated.subject, authenticated.organization)
    await authorized_work(session, actor, grant.work_item_id)
    return endpoint


def token_digest(value: str | None, prefix: str) -> str:
    if value is None or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{43}", value):
        raise reject(401, "invalid preview grant")
    return hash_token(value)


@router.post("/internal/previews/exchange")
async def exchange_preview_grant(
    response: Response, session: SessionDep, config: SettingsDep, _: GatewayDep,
    host: str,
    launch_code: Annotated[str | None, Header(alias="X-Kelpie-Preview-Code")] = None,
    launch_origin: Annotated[str | None, Header(alias="X-Kelpie-Launch-Origin")] = None,
) -> dict:
    require_oidc(config)
    dashboard = urlsplit(config.dashboard_url)
    if launch_origin != f"{dashboard.scheme}://{dashboard.netloc}":
        raise reject(403, "preview launch origin is invalid")
    digest = token_digest(launch_code, "kpl_")
    grant = await session.scalar(select(PreviewGrant).where(PreviewGrant.launch_hash == digest))
    await validate_grant(session, config, grant, host)
    now = datetime.now(UTC)
    token = "kpa_" + secrets.token_urlsafe(32)
    # Conditional UPDATE also fences replays on SQLite, where FOR UPDATE is ignored.
    consumed = await session.scalar(update(PreviewGrant).where(
        PreviewGrant.id == grant.id, PreviewGrant.token_hash.is_(None),
        PreviewGrant.launch_expires_at > now, PreviewGrant.expires_at > now,
    ).values(token_hash=hash_token(token), exchanged_at=now).returning(PreviewGrant.id)
        .execution_options(synchronize_session=False))
    if consumed is None:
        raise reject(401, "preview launch expired or already used")
    await session.commit()
    response.headers.update(NO_STORE)
    return {"token": token, "expires_at": utc(grant.expires_at)}


@router.get("/internal/previews/authorize")
async def authorize_preview_grant(
    response: Response, session: SessionDep, config: SettingsDep, _: GatewayDep,
    host: str,
    token: Annotated[str | None, Header(alias="X-Kelpie-Preview-Token")] = None,
) -> dict:
    require_oidc(config)
    digest = token_digest(token, "kpa_")
    grant = await session.scalar(select(PreviewGrant).where(PreviewGrant.token_hash == digest))
    endpoint = await validate_grant(session, config, grant, host)
    response.headers.update(NO_STORE)
    return {"target_url": endpoint.target_url, "work_item_id": grant.work_item_id,
            "read_only": True, "expires_at": utc(grant.expires_at)}
