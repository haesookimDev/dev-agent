import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_artifact_isolation import artifacts as artifacts
from test_authorization import authorized as authorized
from test_authorization import database, sign_in

from app import artifact_content
from app.models import AgentEvent, Artifact

UNSUPPORTED = ["text/html", "image/svg+xml", "application/xhtml+xml", "application/pdf",
               "text/javascript", "application/octet-stream"]
SUPPORTED = [
    ("image/png", b"\x89PNG\r\n\x1a\n", b"not a PNG"),
    ("image/jpeg", b"\xff\xd8\xff", b"not a JPEG"),
    ("image/webp", b"RIFF\x04\x00\x00\x00WEBP", b"not a WebP"),
    ("text/plain", b"<script>document.title='synthetic probe'</script>", b"\xff"),
    ("application/json", b'{"message":"<script>synthetic</script>"}', b"invalid json"),
]


def metadata(key, content_type):
    return {"kind": "evidence", "name": "evidence.txt", "content_type": content_type,
            "object_key": key, "size_bytes": 1}


@pytest.mark.parametrize("content_type", UNSUPPORTED)
async def test_registration_rejects_unsupported_types_without_metadata_or_events(
    artifacts, content_type,
):
    case = artifacts
    async with database() as session:
        rows = list(await session.scalars(select(Artifact.id)))
        events = list(await session.scalars(select(AgentEvent.id)))
    response = await case.client.post(f"/api/runs/{case.own}/artifacts",
        headers=case.leases[case.own],
        json=metadata(case.rows[case.own].key, content_type))
    assert response.status_code == 415
    assert response.json() == {"detail": "unsupported artifact type"}
    async with database() as session:
        assert list(await session.scalars(select(Artifact.id))) == rows
        assert list(await session.scalars(select(AgentEvent.id))) == events


@pytest.mark.parametrize("content_type", UNSUPPORTED)
async def test_retained_unsupported_type_is_not_served_or_rewritten(artifacts, content_type):
    case = artifacts
    async with database() as session:
        row = Artifact(work_item_id=case.own,
                       **metadata(case.rows[case.own].key, content_type))
        session.add(row)
        await session.commit()
        artifact_id = row.id
    url = f"/api/work-items/{case.own}/artifacts/{artifact_id}"
    response = await case.client.get(url)
    assert response.status_code == 410
    assert response.json() == {"detail": "artifact content is unavailable"}
    async with database() as session:
        assert (await session.get(Artifact, artifact_id)).content_type == content_type
    await sign_in(case.client, organization="other")
    assert (await case.client.get(url)).status_code == 404


@pytest.mark.parametrize("content_type,content,invalid", SUPPORTED)
async def test_supported_upload_and_metadata_alias_remain_inert_and_readable(
    artifacts, content_type, content, invalid,
):
    case = artifacts
    response = await case.client.post(f"/api/runs/{case.own}/artifacts/upload",
        headers=case.leases[case.own],
        params={"name": "evidence.bin", "content_type": content_type}, content=content)
    assert response.status_code == 201
    artifact_id = response.json()["id"]
    async with database() as session:
        key = (await session.get(Artifact, artifact_id)).object_key
    alias = await case.client.post(f"/api/runs/{case.own}/artifacts",
        headers=case.leases[case.own], json=metadata(key, content_type))
    assert alias.status_code == 201
    for identity in (artifact_id, alias.json()["id"]):
        download = await case.client.get(f"/api/work-items/{case.own}/artifacts/{identity}")
        assert download.status_code == 200 and download.content == content
        assert download.headers["content-type"].split(";")[0] == content_type
        assert download.headers["x-content-type-options"] == "nosniff"
        assert download.headers.get("content-security-policy") == "sandbox"


@pytest.mark.parametrize("content_type,content,invalid", SUPPORTED)
async def test_content_type_is_rechecked_after_upload(artifacts, content_type, content, invalid):
    case = artifacts
    params = {"name": "evidence.bin", "content_type": content_type}
    url = f"/api/runs/{case.own}/artifacts/upload"
    rejected = await case.client.post(url, headers=case.leases[case.own], params=params,
                                      content=invalid)
    assert rejected.status_code == 422
    response = await case.client.post(url, headers=case.leases[case.own], params=params,
                                     content=content)
    assert response.status_code == 201
    artifact_id = response.json()["id"]
    async with database() as session:
        key = (await session.get(Artifact, artifact_id)).object_key
    await asyncio.to_thread((case.root / key).write_bytes, invalid)
    response = await case.client.get(f"/api/work-items/{case.own}/artifacts/{artifact_id}")
    assert response.status_code == 410
    assert response.json() == {"detail": "artifact content is unavailable"}


async def test_wrong_lease_still_precedes_content_policy(artifacts):
    case = artifacts
    response = await case.client.post(f"/api/runs/{case.own}/artifacts",
        headers=case.leases[case.foreign],
        json=metadata(case.rows[case.own].key, "text/html"))
    assert response.status_code == 401


async def test_json_parser_failure_is_rejected_on_upload_and_read(artifacts, monkeypatch):
    case = artifacts
    # Parser recursion limits differ across Python versions; exercise the failure itself.
    def failed_parser(_):
        raise RecursionError("synthetic parser detail")
    monkeypatch.setattr(artifact_content, "json",
                        SimpleNamespace(loads=failed_parser, JSONDecodeError=json.JSONDecodeError))
    content = b"[0]"
    response = await case.client.post(f"/api/runs/{case.own}/artifacts/upload",
        headers=case.leases[case.own],
        params={"name": "nested.json", "content_type": "application/json"}, content=content)
    assert response.status_code == 422
    assert response.json() == {"detail": "artifact content does not match its declared type"}
    existing = case.rows[case.own]
    await asyncio.to_thread((case.root / existing.key).write_bytes, content)
    async with database() as session:
        (await session.get(Artifact, existing.id)).content_type = "application/json"
        await session.commit()
    response = await case.client.get(f"/api/work-items/{case.own}/artifacts/{existing.id}")
    assert response.status_code == 410
    assert response.json() == {"detail": "artifact content is unavailable"}
