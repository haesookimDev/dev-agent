import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, WorkerCredential, WorkerCredentialEvent, WorkerHost, utcnow
from app.worker_credentials import (
    authenticate_worker,
    aware,
    issue_credential,
    revoke_credential,
    rotate_credential,
)


@pytest.fixture
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'credentials.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            yield session
    finally:
        await engine.dispose()


async def issue(session, name="worker-a"):
    return await issue_credential(session, name, actor="uid:1000", reason="test provisioning")


async def test_issue_binds_worker_and_persists_only_hash(database):
    first, second = await issue(database), await issue(database, "worker-b")
    await database.commit()
    assert first.worker_id != second.worker_id
    assert first.token not in repr(first)
    stored = await database.get(WorkerCredential, first.credential_id)
    assert stored.token_hash == hashlib.sha256(first.token.encode()).hexdigest()
    assert not hasattr(stored, "token")
    assert (await authenticate_worker(database, first.token)).id == first.worker_id
    assert (await authenticate_worker(database, second.token)).id == second.worker_id
    assert stored.last_used_at is not None
    worker = await database.get(WorkerHost, first.worker_id)
    assert worker.credential_required
    assert worker.active_runs == 0
    assert (await database.scalar(select(WorkerCredentialEvent))).action == "issued"


async def test_rotation_overlaps_without_replacing_worker_identity(database):
    old = await issue(database)
    replacement = await rotate_credential(database, old.credential_id, actor="uid:1000",
                                           reason="scheduled rotation", overlap_seconds=120)
    assert replacement.worker_id == old.worker_id
    assert replacement.credential_id != old.credential_id
    assert (await authenticate_worker(database, old.token)).id == old.worker_id
    assert (await authenticate_worker(database, replacement.token)).id == old.worker_id
    previous = await database.get(WorkerCredential, old.credential_id)
    assert 110 <= (aware(previous.expires_at) - utcnow()).total_seconds() <= 120
    previous.expires_at = utcnow() - timedelta(seconds=1)
    await database.flush()
    assert await authenticate_worker(database, old.token) is None
    assert (await authenticate_worker(database, replacement.token)).id == old.worker_id


async def test_revoke_is_individual_and_idempotent(database):
    first, second = await issue(database), await issue(database, "worker-b")
    for _ in range(2):
        await revoke_credential(database, first.credential_id, actor="uid:1000",
                                reason="replace one credential")
    assert await authenticate_worker(database, first.token) is None
    assert (await authenticate_worker(database, second.token)).id == second.worker_id
    events = list((await database.scalars(select(WorkerCredentialEvent).where(
        WorkerCredentialEvent.action == "revoked",
    ))).all())
    assert len(events) == 1
    with pytest.raises(ValueError, match="active credential"):
        await rotate_credential(database, first.credential_id, actor="uid:1000", reason="rotation")


async def test_unknown_tampered_expired_and_quarantined_credentials_are_rejected(database):
    issued = await issue(database)
    assert await authenticate_worker(database, "unstructured-token") is None
    unknown = "kwc_00000000-0000-0000-0000-000000000000" + issued.token[40:]
    assert await authenticate_worker(database, unknown) is None
    altered = issued.token[:-1] + ("a" if issued.token[-1] != "a" else "b")
    assert await authenticate_worker(database, altered) is None
    worker = await database.get(WorkerHost, issued.worker_id)
    worker.quarantined_at = utcnow()
    await database.flush()
    assert await authenticate_worker(database, issued.token) is None
    with pytest.raises(ValueError, match="quarantined workers"):
        await issue(database)


@pytest.mark.parametrize("kwargs", [
    {"worker_name": ""}, {"actor": ""}, {"reason": ""}, {"lifetime_seconds": 0},
    {"lifetime_seconds": 91 * 86400}, {"reason": "x" * 501},
])
async def test_invalid_provisioning_is_rejected_before_writing(database, kwargs):
    arguments = {"worker_name": "worker-a", "actor": "uid:1000", "reason": "test"} | kwargs
    with pytest.raises(ValueError):
        await issue_credential(database, **arguments)
    assert await database.scalar(select(WorkerHost)) is None


async def test_rotation_failure_does_not_shorten_existing_credential(database):
    issued = await issue(database)
    previous = await database.get(WorkerCredential, issued.credential_id)
    expiry = aware(previous.expires_at)
    with pytest.raises(ValueError, match="overlap"):
        await rotate_credential(database, issued.credential_id, actor="uid:1000", reason="test",
                                overlap_seconds=0)
    assert aware(previous.expires_at) == expiry
    assert len(list((await database.scalars(select(WorkerCredential))).all())) == 1
