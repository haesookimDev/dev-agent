"""Real row-lock regression, enabled by the dedicated PostgreSQL CI step."""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import WorkerCredential, WorkerCredentialEvent, WorkerHost
from app.worker_credentials import authenticate_worker, issue_credential, revoke_credential

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
