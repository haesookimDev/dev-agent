"""Preview grant races on real PostgreSQL connections, in disposable UUID schemas."""

import asyncio
import os
import uuid

import pytest
from fastapi import Response
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_authorization import authorized as authorized
from test_authorization import database
from test_preview_access import access, exchange
from test_preview_access import preview as preview
from test_preview_access import (
    test_current_identity_and_resource_policy_are_rechecked as test_policy_rechecks,
)
from test_preview_access import (
    test_grant_never_outlives_parent_session_or_preview as test_expiry_bounds,
)
from test_preview_access import (
    test_simultaneous_exchanges_issue_exactly_one_token as test_single_exchange,
)
from test_preview_access import (
    test_viewer_can_open_only_the_scoped_preview_without_leaking_credentials as test_viewer_access,
)

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Base, WorkerHost, WorkItem
from app.preview_access import authorize_preview_grant
from app.worker_quarantine import quarantine_worker

DATABASE_URL = os.environ.get("KELPIE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="dedicated PostgreSQL test URL not set")
__all__ = [
    "test_policy_rechecks", "test_expiry_bounds", "test_single_exchange", "test_viewer_access",
]


@pytest.fixture
async def client():
    schema = "preview_test_" + uuid.uuid4().hex
    control = create_async_engine(DATABASE_URL)
    engine = create_async_engine(DATABASE_URL, connect_args={
        "server_settings": {"search_path": schema},
    })
    try:
        async with control.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def override_session():
            async with sessions() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
            yield value
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_settings, None)
        await engine.dispose()
        async with control.begin() as connection:
            # Only this test's generated schema, never the migrated/public schema or user rows.
            await connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await control.dispose()


async def test_grant_waiting_on_quarantine_reads_committed_state(preview, gateway_headers):
    _, _, item, _ = preview
    token = (await exchange(preview, gateway_headers)).json()["token"]
    async with database() as quarantining:
        work = await quarantining.get(WorkItem, item["id"])
        await quarantine_worker(quarantining, work.assigned_worker_id,
                                actor="test", reason="preview lock regression")
        pending = asyncio.create_task(access(preview, gateway_headers, token))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), timeout=0.1)
            await quarantining.commit()
            assert (await asyncio.wait_for(pending, timeout=3)).status_code == 410
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_grant_resolution_serializes_quarantine(preview, gateway_headers):
    _, config, _, host = preview
    token = (await exchange(preview, gateway_headers)).json()["token"]

    async def quarantine():
        async with database() as session:
            worker = await session.scalar(select(WorkerHost))
            await quarantine_worker(session, worker.id, actor="test", reason="preview fence")
            await session.commit()

    async with database() as resolved:
        result = await authorize_preview_grant(Response(), resolved, config, None, host, token)
        assert result["target_url"] == "http://10.0.0.2:3000"
        pending = asyncio.create_task(quarantine())
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), timeout=0.1)
            await resolved.commit()
            await asyncio.wait_for(pending, timeout=3)
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    assert (await access(preview, gateway_headers, token)).status_code == 410
