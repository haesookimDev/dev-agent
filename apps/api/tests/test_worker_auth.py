from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_api import create_work

from app.config import Settings, get_settings
from app.db import get_session
from app.main import app
from app.models import WorkerCredential, WorkerHost, utcnow
from app.worker_credentials import issue_credential, revoke_credential, rotate_credential

RESOURCES = {"cpu": 2, "memory_mb": 4096, "disk_gb": 30}
HEARTBEAT = {"state": "online", "cpu_available": 2, "memory_mb_available": 4096,
             "disk_gb_available": 30, "active_runs": 0}


def headers(token):
    return {"Authorization": f"Bearer {token}"}


async def manage(function, *args, **kwargs):
    async for session in app.dependency_overrides[get_session]():
        result = await function(session, *args, actor="test", reason="test", **kwargs)
        await session.commit()
        return result


async def register(client, name, token):
    return await client.post("/api/workers/register", headers=headers(token), json={
        "name": name, "cpu_total": 2, "memory_mb_total": 4096,
        "disk_gb_available": 30, "labels": {"virtualization": "mock"},
    })


async def test_worker_identity_is_bound_for_registration_heartbeat_and_claim(client):
    first = await manage(issue_credential, "worker-a")
    second = await manage(issue_credential, "worker-b")
    for name, credential in (("worker-a", first), ("worker-b", second)):
        response = await register(client, name, credential.token)
        assert response.status_code == 200
        assert response.json()["cpu_available"] == 2
        assert response.json()["state"] == "online"
    assert (await register(client, "worker-b", first.token)).status_code == 403
    assert (await register(client, "unregistered", first.token)).status_code == 403
    work = await create_work(client)
    for operation, body in (("heartbeat", HEARTBEAT), ("claim", RESOURCES)):
        denied = await client.post(f"/api/workers/{second.worker_id}/{operation}",
                                   headers=headers(first.token), json=body)
        assert denied.status_code == 403
        assert first.token not in denied.text
    claim = await client.post(f"/api/workers/{first.worker_id}/claim",
                               headers=headers(first.token), json=RESOURCES)
    assert claim.status_code == 200
    assert claim.json()["work_item"]["id"] == work["id"]
    assert claim.json()["work_item"]["assigned_worker_id"] == first.worker_id
    # Re-registration must not restore resources still reserved by a live run.
    registered = (await register(client, "worker-a", first.token)).json()
    assert registered["cpu_available"] == registered["memory_mb_available"] == 0
    assert registered["disk_gb_available"] == 0
    assert registered["active_runs"] == 1


async def test_rotation_and_individual_revoke_preserve_active_run_and_other_worker(client):
    first = await manage(issue_credential, "worker-a")
    second = await manage(issue_credential, "worker-b")
    await register(client, "worker-a", first.token)
    await register(client, "worker-b", second.token)
    work = await create_work(client)
    claim = (await client.post(f"/api/workers/{first.worker_id}/claim",
                              headers=headers(first.token), json=RESOURCES)).json()
    lease = {"X-Kelpie-Lease": claim["lease_token"]}
    replacement = await manage(rotate_credential, first.credential_id, overlap_seconds=60)
    assert (await register(client, "worker-a", replacement.token)).status_code == 200
    assert (await register(client, "worker-a", first.token)).status_code == 200
    await manage(revoke_credential, first.credential_id)
    assert (await register(client, "worker-a", first.token)).status_code == 401
    assert (await register(client, "worker-a", replacement.token)).status_code == 200
    assert (await register(client, "worker-b", second.token)).status_code == 200
    assert (await client.get(f"/api/runs/{work['id']}", headers=lease)).status_code == 200
    following = await create_work(client, title="Other worker remains available")
    claimed = await client.post(f"/api/workers/{second.worker_id}/claim",
                                headers=headers(second.token), json=RESOURCES)
    assert claimed.json()["work_item"]["id"] == following["id"]
    async for session in app.dependency_overrides[get_session]():
        credential = await session.get(WorkerCredential, replacement.credential_id)
        credential.expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()
    assert (await register(client, "worker-a", replacement.token)).status_code == 401
    assert (await client.get(f"/api/runs/{work['id']}", headers=lease)).status_code == 200


async def test_shared_token_is_rejected_by_default_and_cannot_reclaim_migrated_worker(client):
    shared = "development-worker-secret-change-me"
    assert Settings(_env_file=None).worker_auth_mode == "scoped"
    assert (await register(client, "legacy", shared)).status_code == 401
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, worker_auth_mode="development",
    )
    assert (await register(client, "legacy", shared)).status_code == 200
    credential = await manage(issue_credential, "legacy")
    assert (await register(client, "legacy", credential.token)).status_code == 200
    await manage(revoke_credential, credential.credential_id)
    assert (await register(client, "legacy", shared)).status_code == 403
    assert (await register(client, "legacy", credential.token)).status_code == 401
    for operation, body in (("heartbeat", HEARTBEAT), ("claim", RESOURCES)):
        response = await client.post(f"/api/workers/{credential.worker_id}/{operation}",
                                      headers=headers(shared), json=body)
        assert response.status_code == 403
    # Development mode remains available only for separate, never-migrated demo workers.
    assert (await register(client, "other-demo", shared)).status_code == 200


async def test_quarantined_identity_cannot_be_reactivated_and_draining_cannot_claim(client):
    credential = await manage(issue_credential, "worker-a")
    await register(client, "worker-a", credential.token)
    await create_work(client)
    draining = {**HEARTBEAT, "state": "draining"}
    assert (await client.post(f"/api/workers/{credential.worker_id}/heartbeat",
                             headers=headers(credential.token), json=draining)).status_code == 200
    claim = await client.post(f"/api/workers/{credential.worker_id}/claim",
                               headers=headers(credential.token), json=RESOURCES)
    assert claim.status_code == 200 and claim.json() is None
    async for session in app.dependency_overrides[get_session]():
        worker = await session.get(WorkerHost, credential.worker_id)
        worker.quarantined_at = utcnow()
        await session.commit()
    assert (await register(client, "worker-a", credential.token)).status_code == 401
    assert (await client.post(f"/api/workers/{credential.worker_id}/heartbeat",
                             headers=headers(credential.token), json=HEARTBEAT)).status_code == 401


async def test_worker_and_gateway_credentials_are_not_interchangeable(client, gateway_headers):
    credential = await manage(issue_credential, "worker-a")
    assert (await client.get("/internal/previews/resolve", params={"host": "missing"},
                             headers=headers(credential.token))).status_code == 401
    assert (await register(client, "worker-a",
                           gateway_headers["Authorization"][7:])).status_code == 401
    assert (await client.get("/internal/previews/resolve", params={"host": "missing"},
                             headers=gateway_headers)).status_code == 404


async def test_gateway_file_rotation_and_configuration_fail_closed(client, tmp_path):
    source = tmp_path / "gateway-token"
    original, rotated = "a" * 64, "b" * 64
    source.write_text(original)
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, gateway_secret_file=str(source), gateway_secret=original,
    )

    async def resolve(token):
        return await client.get("/internal/previews/resolve", params={"host": "missing"},
                                 headers=headers(token))

    assert (await resolve(original)).status_code == 404
    source.write_text(rotated)
    assert (await resolve(original)).status_code == 401
    assert (await resolve(rotated)).status_code == 404
    source.unlink()
    response = await resolve(original)
    assert response.status_code == 503
    assert str(source) not in response.text and original not in response.text
    for invalid in ("short", "kwc_" + original, original + "\ninjected"):
        source.write_text(invalid)
        assert (await resolve(original)).status_code == 503


def test_production_mode_cannot_enable_shared_worker_authentication():
    with pytest.raises(ValidationError, match="restricted to development"):
        Settings(_env_file=None, auth_mode="oidc", worker_auth_mode="development")
