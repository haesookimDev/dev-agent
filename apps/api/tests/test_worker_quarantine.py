import pytest
from sqlalchemy import func, select
from test_api import create_work, register_worker
from test_worker_credentials import database as database

from app.db import get_session
from app.main import app
from app.models import (
    AgentEvent,
    ConsoleLease,
    DeliveryJob,
    PreviewEndpoint,
    ResourceLease,
    WorkerCredentialEvent,
    WorkerHost,
    WorkItem,
    WorkSource,
    WorkStatus,
    utcnow,
)
from app.worker_credentials import authenticate_worker, issue_credential, rotate_credential
from app.worker_quarantine import quarantine_worker


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


@pytest.mark.parametrize("initial", list(WorkStatus))
async def test_quarantine_stops_work_revokes_all_credentials_and_holds_resources(database, initial):
    first = await issue_credential(database, "worker-a", actor="test", reason="test")
    replacement = await rotate_credential(
        database, first.credential_id, actor="test", reason="test",
    )
    other = await issue_credential(database, "worker-b", actor="test", reason="test")
    worker = await database.get(WorkerHost, first.worker_id)
    worker.active_runs = 1
    worker.cpu_total, worker.cpu_available = 4, 2
    worker.memory_mb_total, worker.memory_mb_available = 8192, 4096
    worker.disk_gb_available = 70
    work = WorkItem(source=WorkSource.WEB, title="isolated test", requirement="test",
                    repository="acme/test", assigned_worker_id=worker.id, status=initial)
    database.add(work)
    await database.flush()
    lease = ResourceLease(work_item_id=work.id, worker_id=worker.id, token_hash="test-hash",
                          expires_at=utcnow())
    job = DeliveryJob(work_item_id=work.id, state="running")
    preview = PreviewEndpoint(work_item_id=work.id, hostname="test.preview.localhost",
                              target_url="http://10.0.0.2:3000", expires_at=utcnow())
    console = ConsoleLease(work_item_id=work.id, holder_type="user", holder="viewer",
                           expires_at=utcnow())
    database.add_all([lease, job, preview, console])
    await database.commit()
    result = await quarantine_worker(database, worker.id, actor="uid:1000", reason="incident test")
    await database.commit()
    assert result.revoked_credentials == 2 and result.invalidated_leases == 1
    assert result.affected_work_ids == (work.id,)
    assert worker.quarantined_at is not None and worker.state == "offline"
    assert (worker.active_runs, worker.cpu_available, worker.memory_mb_available,
            worker.disk_gb_available) == (1, 2, 4096, 70)
    assert lease.state == job.state == "quarantined"
    assert preview.expires_at == worker.quarantined_at
    assert console.version == 2
    if initial in {WorkStatus.FAILED, WorkStatus.COMPLETED, WorkStatus.CANCELLED}:
        assert work.status == initial
    elif initial in {WorkStatus.COMMITTING, WorkStatus.PR_CREATED}:
        assert work.status == WorkStatus.FAILED
    else:
        assert work.status == WorkStatus.CANCELLED
    assert await authenticate_worker(database, first.token) is None
    assert await authenticate_worker(database, replacement.token) is None
    assert (await authenticate_worker(database, other.token)).id == other.worker_id
    again = await quarantine_worker(database, worker.id, actor="uid:1000", reason="incident test")
    assert again.already_quarantined and again.invalidated_leases == again.revoked_credentials == 0
    assert await database.scalar(select(func.count()).select_from(WorkerCredentialEvent).where(
        WorkerCredentialEvent.action == "quarantined",
    )) == 1
    assert await database.scalar(select(func.count()).select_from(AgentEvent).where(
        AgentEvent.event_type == "worker.quarantined",
    )) == 1


async def test_quarantine_invalid_target_or_reason_does_not_write(database):
    credential = await issue_credential(database, "worker-a", actor="test", reason="test")
    await database.commit()
    for worker_id, reason in ((credential.worker_id, ""), ("unknown", "test")):
        with pytest.raises(ValueError):
            await quarantine_worker(database, worker_id, actor="test", reason=reason)
    assert (await authenticate_worker(database, credential.token)).id == credential.worker_id
    assert await database.scalar(select(func.count()).select_from(WorkerCredentialEvent).where(
        WorkerCredentialEvent.action == "quarantined",
    )) == 0
