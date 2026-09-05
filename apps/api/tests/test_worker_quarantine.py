import pytest
from test_api import create_work, register_worker

from app.db import get_session
from app.main import app
from app.models import WorkerHost, utcnow


async def assigned_work(client, worker_headers):
    worker = await register_worker(client, worker_headers)
    work = await create_work(client)
    response = await client.post(f"/api/workers/{worker['id']}/claim", headers=worker_headers,
                                 json={"cpu": 2, "memory_mb": 4096, "disk_gb": 30})
    assert response.status_code == 200
    return worker, work, {"X-Kelpie-Lease": response.json()["lease_token"]}


@pytest.mark.parametrize("method,path,body", [
    ("GET", "", None),
    ("GET", "/commands", None),
    ("POST", "/events", {"event_type": "late.event", "message": "must not be accepted"}),
    ("POST", "/transition", {"status": "analyzing", "expected_version": 2}),
    ("POST", "/release", None),
    ("POST", "/preview", {"target_url": "http://10.0.0.2:3000", "ttl_seconds": 3600}),
    ("POST", "/delivery-bundle", None),
    ("POST", "/artifacts/upload?name=evidence.txt&content_type=text/plain", None),
    ("POST", "/artifacts", {"kind": "evidence", "name": "test.txt", "content_type": "text/plain",
                            "object_key": "test.txt", "size_bytes": 0}),
])
async def test_quarantine_flag_fences_every_lease_endpoint(
    client, worker_headers, method, path, body,
):
    worker, work, lease = await assigned_work(client, worker_headers)
    assert (await client.get(f"/api/runs/{work['id']}", headers=lease)).status_code == 200
    async for session in app.dependency_overrides[get_session]():
        host = await session.get(WorkerHost, worker["id"])
        host.quarantined_at = utcnow()
        await session.commit()
    response = await client.request(method, f"/api/runs/{work['id']}{path}",
                                     headers=lease, json=body)
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid lease token"}
    assert lease["X-Kelpie-Lease"] not in response.text
    current = (await client.get(f"/api/work-items/{work['id']}")).json()
    assert current["status"] == "provisioning"
