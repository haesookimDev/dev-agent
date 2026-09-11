"""Lease identifiers correlate recovery state; they are never credentials."""

import asyncio
import socket
import uuid

import uvicorn
from httpx import AsyncClient
from sqlalchemy import select
from test_api import create_work, register_worker

from app.db import get_session
from app.main import app
from app.models import ResourceLease


async def test_claim_returns_persisted_lease_id_without_expanding_lease_authority(
    client, worker_headers,
):
    work = await create_work(client)
    worker = await register_worker(client, worker_headers)
    response = await client.post(
        f"/api/workers/{worker['id']}/claim", headers=worker_headers, json={},
    )
    assert response.status_code == 200
    claim = response.json()
    lease_id = claim["lease_id"]
    assert str(uuid.UUID(lease_id)) == lease_id
    assert lease_id != work["id"]
    async for session in app.dependency_overrides[get_session]():
        lease = await session.scalar(select(ResourceLease).where(
            ResourceLease.work_item_id == work["id"],
        ))
        assert lease.id == lease_id
        assert lease.worker_id == worker["id"]
    # Knowing the public identifier must not grant the per-work token's access.
    denied = await client.get(
        f"/api/runs/{work['id']}", headers={"X-Kelpie-Lease": lease_id},
    )
    assert denied.status_code == 401
    allowed = await client.get(
        f"/api/runs/{work['id']}", headers={"X-Kelpie-Lease": claim["lease_token"]},
    )
    assert allowed.status_code == 200
    assert allowed.json()["id"] == work["id"]


async def test_empty_claim_has_no_lease_identity(client, worker_headers):
    worker = await register_worker(client, worker_headers)
    response = await client.post(
        f"/api/workers/{worker['id']}/claim", headers=worker_headers, json={},
    )
    assert response.status_code == 200
    assert response.json() is None


async def test_real_http_claim_preserves_identity_and_token_boundary(client, worker_headers):
    # Reuse isolated SQLite/settings and real scoped credentials, but exercise
    # the actual API over loopback TCP, not an ASGITransport response fixture.
    # Lifespan is off because this test does not exercise production DB startup.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, log_config=None, access_log=False, lifespan="off", log_level="critical",
    ))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("HTTP server exited before startup")
                await asyncio.sleep(0.01)
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            await create_work(http)
            worker = await register_worker(http, worker_headers)
            response = await http.post(
                f"/api/workers/{worker['id']}/claim", headers=worker_headers, json={},
            )
            assert response.status_code == 200
            claim = response.json()
            assert str(uuid.UUID(claim["lease_id"])) == claim["lease_id"]
            path = f"/api/runs/{claim['work_item']['id']}"
            denied = await http.get(path, headers={"X-Kelpie-Lease": claim["lease_id"]})
            assert denied.status_code == 401
            allowed = await http.get(path, headers={"X-Kelpie-Lease": claim["lease_token"]})
            assert allowed.status_code == 200
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(serving), timeout=3)
        finally:
            if not serving.done():
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)
            listener.close()
