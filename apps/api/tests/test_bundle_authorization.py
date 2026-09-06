import asyncio
from pathlib import Path

import pytest
from delivery_fixtures import PATCH_CONTENT
from sqlalchemy import select
from test_approval_audit import prepare_work
from test_authorization import authorized as authorized
from test_authorization import database, sign_in
from test_integration_authorization import slack_request

from app.models import AgentEvent, Approval, AuditRecord, DeliveryBundle, DeliveryJob, WorkItem


@pytest.mark.parametrize("change", ["missing", "corrupt", "size", "symlink", "outside"])
@pytest.mark.parametrize("transport", ["web", "slack"])
async def test_invalid_bundle_cannot_be_downloaded_or_approved(authorized, monkeypatch,
                                                             change, transport):
    item, deliveries = await prepare_work(authorized, monkeypatch)
    url = f"/api/work-items/{item['id']}"
    assert (await authorized.get(f"{url}/delivery-bundle")).content == PATCH_CONTENT
    async with database() as session:
        bundle = await session.get(DeliveryBundle, item["id"])
        path = Path(bundle.object_path)
        if change == "missing":
            await asyncio.to_thread(path.unlink)
        elif change == "corrupt":
            await asyncio.to_thread(path.write_bytes, b"x" * len(PATCH_CONTENT))
        elif change == "size":
            bundle.size_bytes += 1
        elif change == "outside":
            bundle.object_path = str(path.parent.parent.parent / "private.patch")
        elif change == "symlink":
            target = path.with_name("private.patch")
            await asyncio.to_thread(path.rename, target)
            await asyncio.to_thread(path.symlink_to, target)
        await session.commit()
        events_before = list(await session.scalars(select(AgentEvent.id)))
    response = await authorized.get(f"{url}/delivery-bundle")
    assert response.status_code == 410
    assert response.json() == {"detail": "delivery bundle is unavailable"}
    if transport == "slack":
        approval = await slack_request(authorized, f"approve {item['id']}")
    else:
        approval = await authorized.post(f"{url}/approvals", json={
            "kind": "pull_request", "decision": "approve",
        })
    assert approval.status_code == 409
    assert approval.json() == {"detail": "delivery bundle is unavailable or invalid"}
    async with database() as session:
        work = await session.get(WorkItem, item["id"])
        assert work.status.value == item["status"] and work.version == item["version"]
        for model in (Approval, AuditRecord, DeliveryJob):
            assert not list(await session.scalars(select(model)))
        assert list(await session.scalars(select(AgentEvent.id))) == events_before
    assert deliveries == []
    await sign_in(authorized, "admin", "other")
    assert (await authorized.get(f"{url}/delivery-bundle")).status_code == 404
    assert (await authorized.post(f"{url}/approvals", json={
        "kind": "pull_request", "decision": "approve",
    })).status_code == 404
