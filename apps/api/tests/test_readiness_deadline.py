import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app import db


class StalledEngine:
    def __init__(self, phase):
        self.phase = phase
        self.started = asyncio.Event()
        self.released = False

    async def stall(self):
        self.started.set()
        await asyncio.Event().wait()

    @asynccontextmanager
    async def connect(self):
        try:
            if self.phase == "connection":
                await self.stall()
            yield self
        finally:
            self.released = True

    async def run_sync(self, _):
        await self.stall()


@pytest.mark.parametrize("phase", ["connection", "query"])
async def test_readiness_deadline_bounds_connection_and_schema_query(monkeypatch, phase):
    monkeypatch.setattr(db, "SCHEMA_READINESS_SECONDS", 0.02, raising=False)
    engine = StalledEngine(phase)
    result = await asyncio.wait_for(db.inspect_schema(engine), timeout=0.5)
    assert result.state is db.SchemaState.UNREACHABLE
    assert not result.ready
    assert result.current_heads == ()
    assert result.expected_heads == tuple(sorted(db.migration_scripts().get_heads()))
    assert engine.released


@pytest.mark.parametrize("phase", ["connection", "query"])
async def test_external_cancellation_is_not_converted_to_unreachable(monkeypatch, phase):
    monkeypatch.setattr(db, "SCHEMA_READINESS_SECONDS", 1, raising=False)
    engine = StalledEngine(phase)
    task = asyncio.create_task(db.inspect_schema(engine))
    try:
        await asyncio.wait_for(engine.started.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert engine.released
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_real_unresponsive_postgres_peer_is_bounded_and_disconnected(monkeypatch):
    monkeypatch.setattr(db, "SCHEMA_READINESS_SECONDS", 0.05, raising=False)
    peers = []
    disconnected = asyncio.Event()

    async def hold(reader, writer):
        peers.append(writer)
        try:
            while await reader.read(8192):
                pass
        finally:
            writer.close()
            disconnected.set()

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    engine = create_async_engine(
        f"postgresql+asyncpg://probe:synthetic-test-only@127.0.0.1:{port}/probe",
    )
    try:
        result = await asyncio.wait_for(db.inspect_schema(engine), timeout=0.5)
        assert result.state is db.SchemaState.UNREACHABLE
        assert peers
        await asyncio.wait_for(disconnected.wait(), timeout=0.5)
    finally:
        server.close()
        # Close owned clients before waiting: Python 3.13 waits for active clients too.
        for writer in peers:
            writer.close()
        await asyncio.wait_for(server.wait_closed(), timeout=1)
        await engine.dispose()
