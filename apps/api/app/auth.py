import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db import get_session
from .models import AuthSession, Membership, Organization, Principal


@dataclass(frozen=True)
class Actor:
    subject: str
    role: str
    identity_provider: str
    organization: str
    principal_id: str | None = None


async def actor_from_identity(
    session: AsyncSession, issuer: str, subject: str, organization_claim: str,
) -> Actor:
    row = (await session.execute(
        select(Principal, Organization, Membership)
        .join(Membership, Membership.principal_id == Principal.id)
        .join(Organization, Organization.id == Membership.organization_id)
        .where(Principal.issuer == issuer, Principal.subject == subject,
               Organization.issuer == issuer, Organization.claim == organization_claim)
    )).first()
    if row is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization membership required")
    principal, organization, membership = row
    return Actor(subject=principal.subject, identity_provider=principal.issuer,
                 organization=organization.id, role=membership.role, principal_id=principal.id)


async def current_actor(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Actor:
    if settings.auth_mode == "development":
        organization = await session.get(Organization, settings.development_organization)
        if (settings.development_organization == "legacy"
                or organization is not None and organization.issuer is not None):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "development organization is reserved")
        return Actor(
            subject=settings.development_subject,
            role="administrator",
            identity_provider="development",
            organization=settings.development_organization,
        )

    token = request.cookies.get(settings.oidc_session_cookie_name)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    authenticated = await session.get(AuthSession, hash_token(token))
    if authenticated is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid authentication session")
    expires_at = authenticated.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication session expired")
    if authenticated.identity_provider != settings.oidc_issuer_url:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "identity provider changed")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        dashboard = urlsplit(settings.dashboard_url)
        expected_origin = f"{dashboard.scheme}://{dashboard.netloc}"
        if request.headers.get("origin") != expected_origin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "same-origin request required")
    return await actor_from_identity(session, authenticated.identity_provider,
                                     authenticated.subject, authenticated.organization)


async def require_worker(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = f"Bearer {settings.worker_shared_secret}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid worker credential")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
