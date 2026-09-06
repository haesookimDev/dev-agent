import asyncio

import pytest
from sqlalchemy import delete
from test_artifact_isolation import artifacts as artifacts
from test_authorization import authorized as authorized
from test_authorization import database, sign_in

from app.models import Membership


def assert_private_response(response, expected_status):
    assert response.status_code == expected_status
    assert response.headers.get("cache-control") == "no-store"
    assert "origin" in {field.strip().lower() for field in response.headers["vary"].split(",")}
    assert response.headers.get("x-request-id")


@pytest.mark.parametrize("download", [False, True], ids=["list", "download"])
@pytest.mark.parametrize("identity,status", [
    ("own", 200), ("foreign", 404), ("signed_out", 401), ("revoked", 403),
    ("missing_work", 404),
])
async def test_artifact_reads_and_access_denials_are_not_storable(
    artifacts, download, identity, status,
):
    case = artifacts
    work = "missing-work" if identity == "missing_work" else case.own
    url = f"/api/work-items/{work}/artifacts"
    if download:
        url += f"/{case.rows[case.own].id}"
    if identity == "foreign":
        await sign_in(case.client, organization="other")
    elif identity == "signed_out":
        case.client.cookies.clear()
    elif identity == "revoked":
        async with database() as session:
            await session.execute(delete(Membership).where(Membership.organization_id == "acme"))
            await session.commit()
    # Navigation without Origin must also opt out of storage and vary on Origin.
    case.client.headers.pop("Origin", None)
    response = await case.client.get(url)
    assert_private_response(response, status)
    if status != 200:
        assert "Own evidence" not in response.text
    elif download:
        assert response.content == b"Own evidence\n"
        assert response.headers["content-security-policy"] == "sandbox"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "filename*=" in response.headers["content-disposition"]


@pytest.mark.parametrize("missing", [False, True], ids=["missing_metadata", "missing_file"])
async def test_unavailable_artifacts_are_not_storable(artifacts, missing):
    case = artifacts
    row = case.rows[case.own]
    identity = row.id if missing else "missing-artifact"
    if missing:
        await asyncio.to_thread((case.root / row.key).rename, case.root / "held-evidence")
    response = await case.client.get(f"/api/work-items/{case.own}/artifacts/{identity}")
    assert_private_response(response, 410 if missing else 404)


@pytest.mark.parametrize("origin", ["http://localhost:3000", "https://untrusted.example"])
async def test_artifact_cache_policy_preserves_cors_boundaries(artifacts, origin):
    case = artifacts
    response = await case.client.get(f"/api/work-items/{case.own}/artifacts",
                                     headers={"Origin": origin})
    assert_private_response(response, 200)
    if origin == "http://localhost:3000":
        assert response.headers["access-control-allow-origin"] == origin
        assert response.headers["access-control-allow-credentials"] == "true"
    else:
        assert "access-control-allow-origin" not in response.headers


async def test_artifact_method_denial_and_slash_redirect_are_not_storable(artifacts):
    url = f"/api/work-items/{artifacts.own}/artifacts/{artifacts.rows[artifacts.own].id}"
    assert_private_response(await artifacts.client.head(url), 405)
    assert_private_response(await artifacts.client.get(url + "/"), 307)
