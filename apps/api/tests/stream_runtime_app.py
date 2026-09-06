"""Test-only SQLite/HTTP disconnect probe; never part of the packaged application."""

import threading
from contextvars import ContextVar

from fastapi import Request
from sqlalchemy import event

from app.auth import current_actor
from app.db import SessionLocal, engine
from app.main import SettingsDep, app

in_stream = ContextVar("synthetic_stream_request", default=False)
paused = threading.Event()
released = threading.Event()
armed = False
active = started = closed = pauses = 0


def pause_query():
    paused.set()
    try:
        released.wait(timeout=1)  # A bounded real SQLite query, not an asyncio mock.
        return 1
    finally:
        paused.clear()


@event.listens_for(engine.sync_engine, "connect")
def add_pause_function(connection, _record):
    assert engine.dialect.name == "sqlite", "The disposable stream probe requires SQLite"
    connection.run_async(lambda driver: driver.create_function("stream_pause", 0, pause_query))


@event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
def pause_next_stream_query(_connection, _cursor, statement, parameters, _context, _many):
    global armed, pauses
    if armed and in_stream.get() and "FROM agent_events" in statement:
        armed = False
        pauses += 1
        # Preserve the real event rows/columns while pausing inside the database driver.
        statement = (f"SELECT stream_rows.* FROM ({statement}) AS stream_rows "
                     "CROSS JOIN (SELECT stream_pause() AS ready LIMIT 1) AS stream_gate "
                     "WHERE stream_gate.ready = 1")
    return statement, parameters


class StreamProbe:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        global active, started, closed
        is_stream = scope["type"] == "http" and scope["path"].endswith("/events")
        token = in_stream.set(is_stream)
        if is_stream:
            active += 1
            started += 1
        try:
            await self.app(scope, receive, send)
        finally:
            if is_stream:
                active -= 1
                closed += 1
            in_stream.reset(token)


app.add_middleware(StreamProbe)


async def authorize_probe(request, config):
    # Close the probe's own auth connection before measuring actual pool occupancy.
    async with SessionLocal() as session:
        await current_actor(request, config, session)


@app.get("/__test/stream-state")
async def stream_state(request: Request, config: SettingsDep):
    await authorize_probe(request, config)
    return {"active": active, "started": started, "closed": closed,
            "pauses": pauses, "paused": paused.is_set(), "checked_out": engine.pool.checkedout()}


@app.post("/__test/arm-stream-pause")
async def arm_pause(request: Request, config: SettingsDep):
    global armed
    await authorize_probe(request, config)
    paused.clear()
    released.clear()
    armed = True
    return {"armed": True}


@app.post("/__test/release-stream-pause")
async def release_pause(request: Request, config: SettingsDep):
    await authorize_probe(request, config)
    was_paused = paused.is_set()
    released.set()
    return {"was_paused": was_paused}
