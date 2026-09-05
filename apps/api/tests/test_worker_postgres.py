"""Real row-lock regression, enabled by the dedicated PostgreSQL CI step."""

import asyncio
import os
import secrets
import uuid
from datetime import timedelta

import pytest
from delivery_fixtures import seed_delivery_approval
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import delivery
from app.auth import hash_token
from app.main import resolve_preview
from app.models import (
    AgentEvent,
    ConsoleLease,
    DeliveryBundle,
    DeliveryJob,
    PreviewEndpoint,
    ResourceLease,
    WorkerCredential,
    WorkerCredentialEvent,
    WorkerHost,
    WorkItem,
    WorkSource,
    WorkStatus,
    utcnow,
)
from app.service import validate_lease
from app.worker_credentials import authenticate_worker, issue_credential, revoke_credential
from app.worker_quarantine import quarantine_worker

DATABASE_URL = os.environ.get("KELPIE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="dedicated PostgreSQL test URL not set")


@pytest.fixture
async def credentials():
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    identities = []
    try:
        async with sessions() as session:
            for _ in range(2):
                identities.append(await issue_credential(session, f"pg-test-{uuid.uuid4()}",
                    actor="test", reason="isolated row-lock regression", lifetime_seconds=300))
            await session.commit()
        yield sessions, identities[0], identities[1]
    finally:
        # Remove only the UUID-named workers created by this fixture, never user rows.
        identifiers = [credential.worker_id for credential in identities]
        async with sessions() as session:
            for model in (WorkerCredentialEvent, WorkerCredential):
                await session.execute(delete(model).where(model.worker_id.in_(identifiers)))
            await session.execute(delete(WorkerHost).where(WorkerHost.id.in_(identifiers)))
            await session.commit()
        await engine.dispose()


async def test_inflight_authentication_serializes_revocation_without_blocking_other_worker(
    credentials,
):
    sessions, first, second = credentials
    started = asyncio.Event()

    async def revoke():
        async with sessions() as session:
            started.set()
            await revoke_credential(session, first.credential_id, actor="test", reason="revoke")
            await session.commit()

    async with sessions() as authenticated:
        assert await authenticate_worker(authenticated, first.token) is not None
        revoking = asyncio.create_task(revoke())
        try:
            await started.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(revoking), timeout=0.1)
            async with sessions() as independent:
                assert await asyncio.wait_for(
                    authenticate_worker(independent, second.token), timeout=2,
                ) is not None
                await independent.commit()
            await authenticated.commit()
            await asyncio.wait_for(revoking, timeout=2)
        finally:
            if not revoking.done():
                revoking.cancel()
                await asyncio.gather(revoking, return_exceptions=True)
    async with sessions() as subsequent:
        assert await authenticate_worker(subsequent, first.token) is None


async def test_authentication_waiting_on_revoke_reads_new_committed_state(credentials):
    sessions, first, _ = credentials
    started = asyncio.Event()

    async def authenticate():
        async with sessions() as session:
            started.set()
            return await authenticate_worker(session, first.token)

    async with sessions() as revoking:
        await revoke_credential(revoking, first.credential_id, actor="test", reason="revoke")
        authenticating = asyncio.create_task(authenticate())
        try:
            await started.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(authenticating), timeout=0.1)
            await revoking.commit()
            assert await asyncio.wait_for(authenticating, timeout=2) is None
        finally:
            if not authenticating.done():
                authenticating.cancel()
                await asyncio.gather(authenticating, return_exceptions=True)


@pytest.fixture
async def assigned_runs(credentials):
    sessions, first, second = credentials
    runs = []
    try:
        async with sessions() as session:
            for identity in (first, second):
                work = WorkItem(source=WorkSource.WEB, title="PG quarantine test",
                    requirement="Isolated row-lock regression", repository="acme/pg-test",
                    assigned_worker_id=identity.worker_id, status=WorkStatus.COMMITTING)
                session.add(work)
                await session.flush()
                token = secrets.token_urlsafe(32)
                expiry = utcnow() + timedelta(minutes=5)
                session.add_all([
                    ResourceLease(work_item_id=work.id, worker_id=identity.worker_id,
                                  token_hash=hash_token(token), expires_at=expiry),
                    DeliveryJob(work_item_id=work.id, state="running"),
                    PreviewEndpoint(work_item_id=work.id, hostname=f"{work.id}.preview.localhost",
                                    target_url="http://10.0.0.2:3000", expires_at=expiry),
                    ConsoleLease(work_item_id=work.id, expires_at=expiry),
                ])
                runs.append((work, token))
            await session.commit()
        yield sessions, runs
    finally:
        identifiers = [work.id for work, _ in runs]
        async with sessions() as session:
            for model in (AgentEvent, ConsoleLease, PreviewEndpoint, DeliveryJob, DeliveryBundle,
                          ResourceLease):
                await session.execute(delete(model).where(model.work_item_id.in_(identifiers)))
            await session.execute(delete(WorkItem).where(WorkItem.id.in_(identifiers)))
            await session.commit()


async def access_run(session, run, operation):
    work, token = run
    if operation == "lease":
        return await validate_lease(session, work.id, token, 120)
    if operation == "preview":
        return await resolve_preview(session, None, host=f"{work.id}.preview.localhost")
    return await delivery.lock_delivery(session, work.id)


@pytest.mark.parametrize("operation", ["lease", "preview", "delivery"])
async def test_inflight_access_serializes_quarantine_without_blocking_other_worker(
    assigned_runs, operation,
):
    sessions, runs = assigned_runs
    started = asyncio.Event()

    async def quarantine():
        async with sessions() as session:
            started.set()
            await quarantine_worker(session, runs[0][0].assigned_worker_id,
                                    actor="test", reason="row-lock regression")
            await session.commit()

    async with sessions() as inflight:
        await access_run(inflight, runs[0], operation)
        isolating = asyncio.create_task(quarantine())
        try:
            await started.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(isolating), timeout=0.1)
            async with sessions() as independent:
                await asyncio.wait_for(access_run(independent, runs[1], operation), timeout=2)
                await independent.commit()
            await inflight.commit()
            await asyncio.wait_for(isolating, timeout=2)
        finally:
            if not isolating.done():
                isolating.cancel()
                await asyncio.gather(isolating, return_exceptions=True)
    async with sessions() as subsequent:
        with pytest.raises((HTTPException, delivery.DeliveryStopped)):
            await access_run(subsequent, runs[0], operation)


@pytest.mark.parametrize("operation", ["lease", "preview", "delivery"])
async def test_access_waiting_for_quarantine_reads_committed_denial(assigned_runs, operation):
    sessions, runs = assigned_runs
    started = asyncio.Event()

    async def access():
        async with sessions() as session:
            started.set()
            return await access_run(session, runs[0], operation)

    async with sessions() as isolating:
        await quarantine_worker(isolating, runs[0][0].assigned_worker_id,
                                actor="test", reason="row-lock regression")
        accessing = asyncio.create_task(access())
        try:
            await started.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(accessing), timeout=0.1)
            await isolating.commit()
            with pytest.raises((HTTPException, delivery.DeliveryStopped)) as rejected:
                await asyncio.wait_for(accessing, timeout=2)
            if operation != "delivery":
                assert rejected.value.status_code == (401 if operation == "lease" else 410)
        finally:
            if not accessing.done():
                accessing.cancel()
                await asyncio.gather(accessing, return_exceptions=True)


async def test_publication_timeout_releases_quarantine_lock(assigned_runs, monkeypatch):
    sessions, runs = assigned_runs
    authority_ids = []
    async with sessions() as session:
        for work, _ in runs:
            approval = await seed_delivery_approval(session, work, "0" * 64)
            (await session.get(DeliveryJob, work.id)).approval_audit_id = approval.id
            session.add(DeliveryBundle(work_item_id=work.id, sha256="0" * 64,
                                       object_path="unused-lock-test.patch", size_bytes=0))
            authority_ids.append(approval.id)
        # These two test snapshots remain append-only until the dedicated test DB is removed.
        await session.commit()
    monkeypatch.setattr(delivery, "SessionLocal", sessions)
    monkeypatch.setattr(delivery, "DELIVERY_WRITE_SECONDS", 0.3)
    started = asyncio.Event()

    async def publish():
        async with delivery.guard_delivery_write(runs[0][0].id, approval_audit_id=authority_ids[0]):
            started.set()
            await asyncio.Event().wait()

    publishing = asyncio.create_task(publish())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        async with sessions() as isolating:
            await asyncio.wait_for(quarantine_worker(isolating, runs[0][0].assigned_worker_id,
                actor="test", reason="bounded publication regression"), timeout=2)
            await isolating.commit()
        with pytest.raises(TimeoutError):
            await publishing
        async with delivery.guard_delivery_write(runs[1][0].id, approval_audit_id=authority_ids[1]):
            pass
        with pytest.raises(delivery.DeliveryStopped):
            async with delivery.guard_delivery_write(runs[0][0].id,
                                                     approval_audit_id=authority_ids[0]):
                pytest.fail("publication after quarantine must not start")
    finally:
        publishing.cancel()
        await asyncio.gather(publishing, return_exceptions=True)
