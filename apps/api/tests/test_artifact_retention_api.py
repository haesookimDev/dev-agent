from datetime import timedelta

import pytest
from test_artifact_isolation import artifacts as artifacts
from test_authorization import authorized as authorized
from test_authorization import database, sign_in

from app import main
from app.models import Artifact, utcnow


@pytest.mark.parametrize("purged", [False, True])
async def test_authorized_expired_artifact_is_gone_without_reading_or_exposing_storage(
    artifacts, monkeypatch, purged,
):
    case = artifacts
    row_id = case.rows[case.own].id
    listing = f"/api/work-items/{case.own}/artifacts"
    before = (await case.client.get(listing)).json()
    assert before[0]["expired_at"] is None
    assert (await case.client.get(f"{listing}/{row_id}")).status_code == 200
    async with database() as session:
        row = await session.get(Artifact, row_id)
        row.expired_at = utcnow() - timedelta(seconds=1)
        row.purged_at = utcnow() if purged else None
        row.retention_days, row.retention_sha256 = 30, "a" * 64
        await session.commit()
    # A pending deletion still has bytes; an expired authorization must never read them.
    def forbidden(*_, **__):
        pytest.fail("expired artifact content must not be opened")
    monkeypatch.setattr(main, "read_artifact_content", forbidden)
    await sign_in(case.client, subject="viewer")
    response = await case.client.get(f"{listing}/{row_id}")
    assert response.status_code == 410
    assert response.json() == {"detail": "artifact retention period has expired"}
    assert "no-store" in response.headers["cache-control"]
    listed = await case.client.get(listing)
    assert listed.status_code == 200 and "no-store" in listed.headers["cache-control"]
    value = listed.json()[0]
    assert value["expired_at"] is not None
    assert value.keys() == before[0].keys()
    assert {key: item for key, item in value.items() if key != "expired_at"} == {
        key: item for key, item in before[0].items() if key != "expired_at"}
    assert not {"object_key", "retention_sha256", "purged_at", "retention_days"} & value.keys()
    await sign_in(case.client, organization="other")
    assert (await case.client.get(f"{listing}/{row_id}")).status_code == 404
    assert (await case.client.get(listing)).status_code == 404
    assert (await case.client.get(
        f"/api/work-items/{case.foreign}/artifacts/{row_id}")).status_code == 404
    case.client.cookies.clear()
    assert (await case.client.get(f"{listing}/{row_id}")).status_code == 401
