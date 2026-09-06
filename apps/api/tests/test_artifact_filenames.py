from urllib.parse import quote, unquote_to_bytes

import pytest
from sqlalchemy import select
from test_artifact_isolation import artifacts as artifacts
from test_authorization import authorized as authorized
from test_authorization import database, sign_in

from app.artifact_names import artifact_disposition, valid_artifact_name
from app.models import AgentEvent, Artifact

VALID_NAMES = ["report.txt", "검증 결과 ✅.txt", "résumé.txt", "emoji 👩‍💻.txt",
               "100%20 complete; v2.txt", "owner's file.txt", "a" * 251 + ".txt"]
INVALID_NAMES = ["", " ", ".", "..", "../report.txt", "folder/report.txt", "folder\\report.txt",
                 'report".txt', "bad\r\nX-Synthetic-Probe: 1.txt", "nul\x00.txt", "tab\t.txt",
                 "control\x1f.txt", "c1\u0085.txt", " leading.txt", "trailing.txt "]


@pytest.mark.parametrize("name", ["\ud800.txt", "\udfff.txt", "a" * 256])
def test_unencodable_or_oversized_retained_names_are_bounded(name):
    assert not valid_artifact_name(name)
    assert artifact_disposition(name) == 'inline; filename="artifact"; filename*=UTF-8\'\'artifact'


def metadata(case, name):
    return {"kind": "evidence", "name": name, "content_type": "text/plain",
            "object_key": case.rows[case.own].key, "size_bytes": 13}


def assert_disposition(response, name):
    value = response.headers["content-disposition"]
    fallback = name if name.isascii() and "%" not in name else "artifact"
    assert value == f'inline; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'
    assert value.isascii() and "\r" not in value and "\n" not in value
    assert unquote_to_bytes(value.split("filename*=UTF-8''", 1)[1]) == name.encode("utf-8")
    assert "x-synthetic-probe" not in response.headers
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name", VALID_NAMES)
async def test_upload_and_registration_preserve_international_filenames(artifacts, name):
    case = artifacts
    uploaded = await case.client.post(f"/api/runs/{case.own}/artifacts/upload",
        headers=case.leases[case.own], params={"name": name, "content_type": "text/plain"},
        content=b"Synthetic filename evidence\n")
    assert uploaded.status_code == 201 and uploaded.json()["name"] == name
    registered = await case.client.post(f"/api/runs/{case.own}/artifacts",
        headers=case.leases[case.own], json=metadata(case, name))
    assert registered.status_code == 201
    for identity, content in [(uploaded.json()["id"], b"Synthetic filename evidence\n"),
                              (registered.json()["id"], b"Own evidence\n")]:
        response = await case.client.get(f"/api/work-items/{case.own}/artifacts/{identity}")
        assert response.status_code == 200 and response.content == content
        assert_disposition(response, name)
        async with database() as session:
            assert (await session.get(Artifact, identity)).name == name


@pytest.mark.parametrize("name", INVALID_NAMES)
async def test_invalid_new_filenames_do_not_publish_rows_events_or_files(artifacts, name):
    case = artifacts
    async with database() as session:
        rows = list(await session.scalars(select(Artifact.id)))
        events = list(await session.scalars(select(AgentEvent.id)))
    files = sorted(case.root.rglob("*"))
    registered = await case.client.post(f"/api/runs/{case.own}/artifacts",
        headers=case.leases[case.own], json=metadata(case, name))
    assert registered.status_code == 422
    uploaded = await case.client.post(f"/api/runs/{case.own}/artifacts/upload",
        headers=case.leases[case.own], params={"name": name, "content_type": "text/plain"},
        content=b"Synthetic rejected filename\n")
    assert uploaded.status_code == 422
    async with database() as session:
        assert list(await session.scalars(select(Artifact.id))) == rows
        assert list(await session.scalars(select(AgentEvent.id))) == events
    assert sorted(case.root.rglob("*")) == files


@pytest.mark.parametrize("name", INVALID_NAMES)
async def test_retained_invalid_filename_uses_safe_header_without_rewriting_metadata(
    artifacts, name,
):
    case = artifacts
    async with database() as session:
        row = Artifact(work_item_id=case.own, **metadata(case, name))
        session.add(row)
        await session.commit()
        identity = row.id
    response = await case.client.get(f"/api/work-items/{case.own}/artifacts/{identity}")
    assert response.status_code == 200 and response.content == b"Own evidence\n"
    assert_disposition(response, "artifact")
    async with database() as session:
        assert (await session.get(Artifact, identity)).name == name
    await sign_in(case.client, organization="other")
    response = await case.client.get(f"/api/work-items/{case.own}/artifacts/{identity}")
    assert response.status_code == 404


async def test_filename_validation_does_not_precede_lease_authorization(artifacts):
    case = artifacts
    response = await case.client.post(f"/api/runs/{case.own}/artifacts",
        headers=case.leases[case.foreign], json=metadata(case, "bad\r\n.txt"))
    assert response.status_code == 401
    response = await case.client.post(f"/api/runs/{case.own}/artifacts/upload",
        headers=case.leases[case.foreign],
        params={"name": "bad\r\n.txt", "content_type": "text/plain"}, content=b"Evidence")
    assert response.status_code == 401


@pytest.mark.parametrize("field,value", [("object_key", "other/artifacts/file.txt"),
                                        ("content_type", "text/html")])
async def test_filename_fallback_cannot_bypass_file_or_content_boundaries(artifacts, field, value):
    case = artifacts
    payload = {**metadata(case, "bad\r\n.txt"), field: value}
    async with database() as session:
        row = Artifact(work_item_id=case.own, **payload)
        session.add(row)
        await session.commit()
        identity = row.id
    response = await case.client.get(f"/api/work-items/{case.own}/artifacts/{identity}")
    assert response.status_code == 410
    assert response.json() == {"detail": "artifact content is unavailable"}
