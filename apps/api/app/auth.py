import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db import get_session
from .models import AuthSession


@dataclass(frozen=True)
class Actor:
    subject: str
    role: str
    identity_provider: str
    organization: str


async def current_actor(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Actor:
    if settings.auth_mode == "development":
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
    return Actor(
        subject=authenticated.subject,
        role="viewer",
        identity_provider=authenticated.identity_provider,
        organization=authenticated.organization,
    )


async def require_approver(actor: Annotated[Actor, Depends(current_actor)]) -> Actor:
    if actor.role not in {"administrator", "approver"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "approver role required")
    return actor


async def require_worker(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = f"Bearer {settings.worker_shared_secret}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid worker credential")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
