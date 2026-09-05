"""Control-host credential management; no public issuance endpoint."""

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import WorkerCredential, WorkerCredentialEvent, WorkerHost, WorkerState, utcnow

TOKEN_PATTERN = re.compile(
    r"kwc_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.([0-9a-f]{64})"
)
DEFAULT_LIFETIME_SECONDS = 30 * 86400
MAX_LIFETIME_SECONDS = 90 * 86400


@dataclass(frozen=True)
class IssuedCredential:
    worker_id: str
    credential_id: str
    token: str = field(repr=False)
    expires_at: datetime


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def validate_audit(actor: str, reason: str) -> None:
    if not actor.strip() or len(actor) > 255 or not reason.strip() or len(reason) > 500:
        raise ValueError("credential management requires a bounded actor and reason")


def validate_lifetime(seconds: int) -> None:
    if not 60 <= seconds <= MAX_LIFETIME_SECONDS:
        raise ValueError("credential lifetime must be between 60 seconds and 90 days")


async def lock_worker(session: AsyncSession, worker_id: str) -> WorkerHost:
    worker = await session.scalar(select(WorkerHost).where(WorkerHost.id == worker_id)
                                  .with_for_update().execution_options(populate_existing=True))
    if worker is None:
        raise ValueError("worker not found")
    return worker


async def lock_credential(
    session: AsyncSession, credential_id: str,
) -> tuple[WorkerHost, WorkerCredential]:
    worker_id = await session.scalar(select(WorkerCredential.worker_id)
                                     .where(WorkerCredential.id == credential_id))
    if worker_id is None:
        raise ValueError("credential not found")
    # Every management/authentication path locks the worker first. Read the
    # credential again after obtaining that lock to observe a concurrent revoke.
    worker = await lock_worker(session, worker_id)
    credential = await session.scalar(select(WorkerCredential).where(
        WorkerCredential.id == credential_id, WorkerCredential.worker_id == worker.id,
    ).with_for_update().execution_options(populate_existing=True))
    if credential is None:
        raise ValueError("credential not found")
    return worker, credential


async def authenticate_worker(session: AsyncSession, token: str) -> WorkerHost | None:
    match = TOKEN_PATTERN.fullmatch(token)
    if match is None:
        return None
    try:
        worker, credential = await lock_credential(session, match[1])
    except ValueError:
        return None
    now = utcnow()
    digest = hashlib.sha256(token.encode()).hexdigest()
    if (worker.quarantined_at is not None or credential.revoked_at is not None
            or aware(credential.expires_at) <= now
            or not hmac.compare_digest(credential.token_hash, digest)):
        return None
    credential.last_used_at = now
    return worker


def record_event(session: AsyncSession, worker_id: str, credential_id: str | None,
                 action: str, actor: str, reason: str) -> None:
    session.add(WorkerCredentialEvent(worker_id=worker_id, credential_id=credential_id,
                                      action=action, actor=actor, reason=reason))


async def _mint(session: AsyncSession, worker: WorkerHost, actor: str, reason: str,
               lifetime_seconds: int, action: str) -> IssuedCredential:
    if worker.quarantined_at is not None:
        raise ValueError("quarantined workers cannot receive credentials")
    identifier = str(uuid.uuid4())
    token = f"kwc_{identifier}.{secrets.token_hex(32)}"
    expires_at = utcnow() + timedelta(seconds=lifetime_seconds)
    credential = WorkerCredential(id=identifier, worker_id=worker.id,
        token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=expires_at)
    worker.credential_required = True
    session.add(credential)
    await session.flush()
    record_event(session, worker.id, credential.id, action, actor, reason)
    await session.flush()
    return IssuedCredential(worker.id, credential.id, token, expires_at)


async def issue_credential(session: AsyncSession, worker_name: str, *, actor: str, reason: str,
                           lifetime_seconds: int = DEFAULT_LIFETIME_SECONDS) -> IssuedCredential:
    validate_audit(actor, reason)
    validate_lifetime(lifetime_seconds)
    if not worker_name.strip() or len(worker_name) > 255 or any(
        ord(character) < 32 for character in worker_name
    ):
        raise ValueError("invalid worker name")
    worker = await session.scalar(select(WorkerHost).where(WorkerHost.name == worker_name)
                                  .with_for_update().execution_options(populate_existing=True))
    if worker is None:
        worker = WorkerHost(name=worker_name, state=WorkerState.OFFLINE,
            cpu_total=0, cpu_available=0, memory_mb_total=0, memory_mb_available=0,
            disk_gb_available=0, active_runs=0, labels={})
        session.add(worker)
        await session.flush()
    return await _mint(session, worker, actor, reason, lifetime_seconds, "issued")


async def rotate_credential(session: AsyncSession, credential_id: str, *, actor: str, reason: str,
                            lifetime_seconds: int = DEFAULT_LIFETIME_SECONDS,
                            overlap_seconds: int = 600) -> IssuedCredential:
    validate_audit(actor, reason)
    validate_lifetime(lifetime_seconds)
    if not 60 <= overlap_seconds <= 3600:
        raise ValueError("rotation overlap must be between 60 and 3600 seconds")
    worker, previous = await lock_credential(session, credential_id)
    now = utcnow()
    if previous.revoked_at is not None or aware(previous.expires_at) <= now:
        raise ValueError("only an active credential can be rotated")
    result = await _mint(session, worker, actor, reason, lifetime_seconds, "rotated")
    previous.expires_at = min(aware(previous.expires_at), now + timedelta(seconds=overlap_seconds))
    await session.flush()
    return result


async def revoke_credential(session: AsyncSession, credential_id: str, *, actor: str,
                            reason: str) -> None:
    validate_audit(actor, reason)
    worker, credential = await lock_credential(session, credential_id)
    if credential.revoked_at is not None:
        return
    credential.revoked_at = utcnow()
    record_event(session, worker.id, credential.id, "revoked", actor, reason)
    await session.flush()
