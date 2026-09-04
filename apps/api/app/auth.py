import hashlib
import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from .config import Settings, get_settings


@dataclass(frozen=True)
class Actor:
    subject: str
    role: str


async def current_actor(
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[str | None, Header(alias="X-Kelpie-User")] = None,
    role: Annotated[str | None, Header(alias="X-Kelpie-Role")] = None,
) -> Actor:
    if settings.auth_mode == "development":
        return Actor(subject=user or "local-admin", role=role or "admin")
    if settings.auth_mode != "trusted_headers":
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "unsupported AUTH_MODE")
    if not user or role not in {"admin", "approver", "viewer"}:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing authenticated identity")
    return Actor(subject=user, role=role)


async def require_approver(actor: Annotated[Actor, Depends(current_actor)]) -> Actor:
    if actor.role not in {"admin", "approver"}:
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
