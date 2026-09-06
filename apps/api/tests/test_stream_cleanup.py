import asyncio
import logging
import os
import uuid
from types import SimpleNamespace

import anyio
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request
from test_authorization import authorized as authorized
from test_authorization import create_item, database
from test_oidc import oidc_settings

from app.auth import current_actor
from app.config import get_settings
from app.db import get_session
from app.main import app, stream_events
from app.models import Base


@pytest.fixture(params=["sqlite", "postgres"])
async def client(tmp_path, request):
    postgres = request.param == "postgres"
    url = os.environ.get("KELPIE_TEST_POSTGRES_URL") if postgres else (
        f"sqlite+aiosqlite:///{tmp_path / 'stream.db'}")
    if not url:
        pytest.skip("dedicated PostgreSQL test URL not set")
    schema = f"stream_test_{uuid.uuid4().hex}"
    admin = create_async_engine(url) if postgres else None
    engine = create_async_engine(url, connect_args={
        "server_settings": {"search_path": schema},
    } if postgres else {})
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with factory() as session:
            yield session

    try:
        if admin is not None:
            async with admin.begin() as connection:
                await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_settings] = oidc_settings
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
            yield value
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as connection:
                await connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await admin.dispose()


@pytest.fixture
async def stream_case(authorized, monkeypatch, caplog):
    # Earlier in-process migrations disable existing loggers via Alembic fileConfig.
    for name in ("app.main", "sqlalchemy.pool.impl.AsyncAdaptedQueuePool"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    caplog.set_level(logging.WARNING, logger="app.main")
    item = await create_item(authorized)
    request = Request({"type": "http", "method": "GET", "headers": [
        (b"cookie", f"kelpie_session={authorized.cookies.get('kelpie_session')}".encode()),
    ]})
    sessions = []
    responses = []
    async with database() as session:
        engine = session.bind

    async def open_stream(after=0):
        async with database() as session:
            actor = await current_actor(request, oidc_settings(), session)
            response = await stream_events(request, item["id"], session, actor, oidc_settings(),
                                           after=after, last_event_id=None)
        responses.append(response)
        return response

    factory = async_sessionmaker(engine, expire_on_commit=False)

    def tracked_session():
        value = factory()
        sessions.append(value)
        return value

    monkeypatch.setattr("app.main.SessionLocal", tracked_session)
    try:
        yield SimpleNamespace(engine=engine, open=open_stream)
    finally:
        for response in responses:
            await response.body_iterator.aclose()
        for session in sessions:
            await session.close()


@pytest.mark.parametrize("phase", ["auth_sessions", "agent_events", "rollback"])
async def test_stream_disconnect_during_database_io_returns_connections(stream_case, caplog, phase):
    response = await stream_case.open()
    engine = stream_case.engine
    caplog.set_level(logging.ERROR, logger="sqlalchemy.pool")
    interrupted = False

    def disconnect(_connection, *arguments):
        nonlocal interrupted
        if not interrupted and (phase == "rollback" or f"FROM {phase}" in arguments[1]):
            interrupted = True
            scope.cancel()

    event_name = "rollback" if phase == "rollback" else "before_cursor_execute"
    event.listen(engine.sync_engine, event_name, disconnect)
    try:
        with anyio.CancelScope() as scope:
            await anext(response.body_iterator)
        assert scope.cancelled_caught, "The disconnect cancellation must not be swallowed"
        # Give SQLAlchemy's shielded close task a chance to finish, without masking errors.
        await asyncio.sleep(0.05)
        assert interrupted, "The cancellation must occur during actual database I/O"
        assert engine.pool.checkedout() == 0
        errors = [record.getMessage() for record in caplog.records
                  if record.name.startswith("sqlalchemy.pool") and record.levelno >= logging.ERROR]
        assert errors == [], "Disconnect must not leak or abandon a database connection"
    finally:
        event.remove(engine.sync_engine, event_name, disconnect)


@pytest.mark.parametrize("failure", ["timeout", "database"])
async def test_stream_read_failure_closes_cleanly_without_private_details(
    stream_case, monkeypatch, caplog, failure,
):
    response = await stream_case.open()
    original = AsyncSession.scalars
    reached = asyncio.Event()

    async def failed_read(session, *arguments, **keywords):
        await original(session, *arguments, **keywords)  # Keep an actual connection checked out.
        reached.set()
        if failure == "database":
            raise SQLAlchemyError("synthetic-private-query-detail")
        await asyncio.Event().wait()

    with monkeypatch.context() as change:
        change.setattr(AsyncSession, "scalars", failed_read)
        change.setattr("app.main.STREAM_READ_SECONDS", 0.3)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(response.body_iterator), timeout=1)
    assert reached.is_set()
    assert stream_case.engine.pool.checkedout() == 0
    assert "event stream read failed; closing stream" in caplog.text
    assert "synthetic-private-query-detail" not in caplog.text
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    # A subsequent connection must still deliver authentic events, not a fake success.
    reopened = await stream_case.open()
    assert "work.created" in await anext(reopened.body_iterator)
    assert stream_case.engine.pool.checkedout() == 0


async def test_idle_stream_disconnect_does_not_wait_for_read_deadline(stream_case):
    response = await stream_case.open(after=2**31 - 1)
    async with asyncio.timeout(1):
        with anyio.move_on_after(0.03) as scope:
            await anext(response.body_iterator)
    assert scope.cancelled_caught
    assert stream_case.engine.pool.checkedout() == 0
