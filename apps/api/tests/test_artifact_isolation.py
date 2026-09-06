import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_api import register_worker
from test_authorization import authorized as authorized
from test_authorization import create_item, database, sign_in

from app.config import get_settings
from app.main import app
from app.models import AgentEvent, Artifact, WorkerHost, utcnow


@pytest.fixture
async def artifacts(authorized, worker_headers):
    worker = await register_worker(authorized, worker_headers)
    own = await create_item(authorized)
    await sign_in(authorized, organization="other")
    foreign = await create_item(authorized, "other/service")
    await sign_in(authorized)
    leases = {}
    for _ in range(2):
        claimed = await authorized.post(f"/api/workers/{worker['id']}/claim",
            headers=worker_headers, json={"cpu": 2, "memory_mb": 4096, "disk_gb": 30})
        assert claimed.status_code == 200
        value = claimed.json()
        leases[value["work_item"]["id"]] = {"X-Kelpie-Lease": value["lease_token"]}
    rows = {}
    for item in (own, foreign):
        response = await authorized.post(f"/api/runs/{item['id']}/artifacts/upload",
            headers=leases[item["id"]],
            params={"name": "evidence.txt", "content_type": "text/plain"},
            content=b"Own evidence\n" if item == own else b"Foreign synthetic evidence\n")
        assert response.status_code == 201
        async with database() as session:
            row = await session.get(Artifact, response.json()["id"])
            rows[item["id"]] = SimpleNamespace(id=row.id, key=row.object_key)
    root = Path(app.dependency_overrides[get_settings]().artifact_root)
    return SimpleNamespace(client=authorized, own=own["id"], foreign=foreign["id"],
                           leases=leases, rows=rows, root=root, worker=worker["id"])


def metadata(key):
    return {"kind": "evidence", "name": "alias.txt", "content_type": "text/plain",
            "object_key": key, "size_bytes": 1}


@pytest.mark.parametrize("target", ["foreign", "delivery", "root", "work_prefix"])
async def test_registration_rejects_non_owned_artifact_keys_without_rows_or_events(
    artifacts, target,
):
    case = artifacts
    key = {"foreign": case.rows[case.foreign].key,
           "delivery": f"{case.own}/delivery.patch", "root": "api.log",
           "work_prefix": f"{case.own}-other/artifacts/file.txt"}[target]
    async with database() as session:
        before = list(await session.scalars(select(AgentEvent.id)))
        rows = list(await session.scalars(select(Artifact.id)))
    response = await case.client.post(f"/api/runs/{case.own}/artifacts",
                                     headers=case.leases[case.own], json=metadata(key))
    assert response.status_code == 422
    assert response.json() == {"detail": "artifact key must belong to this work"}
    async with database() as session:
        assert list(await session.scalars(select(Artifact.id))) == rows
        assert list(await session.scalars(select(AgentEvent.id))) == before


@pytest.mark.parametrize("target", ["foreign", "delivery", "symlink", "parent_symlink"])
async def test_retained_metadata_cannot_read_another_storage_namespace(artifacts, target):
    case = artifacts
    foreign = case.rows[case.foreign].key
    if target == "foreign":
        key = foreign
    elif target == "delivery":
        key = f"{case.own}/delivery.patch"
        await asyncio.to_thread((case.root / key).write_bytes, b"Private synthetic delivery\n")
    else:
        key = f"{case.own}/artifacts/linked"
        if target == "symlink":
            await asyncio.to_thread((case.root / key).symlink_to, case.root / foreign)
        else:
            await asyncio.to_thread((case.root / key).symlink_to,
                                    (case.root / foreign).parent, target_is_directory=True)
            key += "/" + Path(foreign).name
    async with database() as session:
        row = Artifact(work_item_id=case.own, **metadata(key))
        session.add(row)
        await session.commit()
        artifact_id = row.id
    response = await case.client.get(f"/api/work-items/{case.own}/artifacts/{artifact_id}")
    assert response.status_code == 410
    assert response.json() == {"detail": "artifact content is unavailable"}
    async with database() as session:
        assert (await session.get(Artifact, artifact_id)).object_key == key


async def test_owned_upload_and_scoped_metadata_remain_readable_but_foreign_org_is_hidden(
    artifacts,
):
    case = artifacts
    own = case.rows[case.own]
    response = await case.client.post(f"/api/runs/{case.own}/artifacts",
        headers=case.leases[case.own], json=metadata(own.key))
    assert response.status_code == 201
    for artifact_id in (own.id, response.json()["id"]):
        url = f"/api/work-items/{case.own}/artifacts/{artifact_id}"
        download = await case.client.get(url)
        assert download.status_code == 200 and download.content == b"Own evidence\n"
        await sign_in(case.client, organization="other")
        assert (await case.client.get(url)).status_code == 404
        await sign_in(case.client)
    # A different run's lease and a quarantined worker cannot register even a well-scoped key.
    rejected = await case.client.post(f"/api/runs/{case.own}/artifacts",
        headers=case.leases[case.foreign], json=metadata(own.key))
    assert rejected.status_code == 401
    async with database() as session:
        (await session.get(WorkerHost, case.worker)).quarantined_at = utcnow()
        await session.commit()
    rejected = await case.client.post(f"/api/runs/{case.own}/artifacts",
        headers=case.leases[case.own], json=metadata(own.key))
    assert rejected.status_code == 401


async def test_upload_does_not_follow_a_work_directory_link_or_publish_metadata(artifacts):
    case = artifacts
    source = case.root / case.own
    await asyncio.to_thread(source.rename, case.root / "original-owned-work")
    foreign = case.root / case.foreign
    await asyncio.to_thread(source.symlink_to, foreign, target_is_directory=True)
    files_before = list((foreign / "artifacts").iterdir())
    async with database() as session:
        rows = list(await session.scalars(select(Artifact.id)))
        events = list(await session.scalars(select(AgentEvent.id)))
    response = await case.client.post(f"/api/runs/{case.own}/artifacts/upload",
        headers=case.leases[case.own], params={"name": "new.txt", "content_type": "text/plain"},
        content=b"Must not reach foreign storage\n")
    assert response.status_code == 503
    assert response.json() == {"detail": "artifact storage is unavailable"}
    assert list((foreign / "artifacts").iterdir()) == files_before
    async with database() as session:
        assert list(await session.scalars(select(Artifact.id))) == rows
        assert list(await session.scalars(select(AgentEvent.id))) == events
