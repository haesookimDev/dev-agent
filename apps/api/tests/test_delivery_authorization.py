from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_approval_audit import prepare_work
from test_authorization import authorized as authorized
from test_authorization import database, sign_in
from test_integration_authorization import slack_request

from app import delivery
from app.models import WorkItem
from app.worker_credentials import issue_credential


@pytest.mark.parametrize("transport", ["web", "slack"])
async def test_retained_approval_identity_is_distinct_from_service_and_admin_only(
    authorized, monkeypatch, transport,
):
    item, _ = await prepare_work(authorized, monkeypatch)
    async with database() as session:
        worker = await issue_credential(session, "delivery", actor="test", reason="test")
        (await session.get(WorkItem, item["id"])).assigned_worker_id = worker.worker_id
        await session.commit()
        sessions = async_sessionmaker(session.bind, expire_on_commit=False)
    monkeypatch.setattr(delivery, "SessionLocal", sessions)
    monkeypatch.setattr("app.main.deliver_work", delivery.deliver_work)
    create_pr = AsyncMock(return_value="https://github.com/acme/service/pull/9")
    monkeypatch.setattr(delivery, "github", SimpleNamespace(
        installation_token=AsyncMock(return_value="synthetic-test-token"),
        repository=AsyncMock(return_value={"default_branch": "main"}),
        find_pull_request=AsyncMock(return_value=None), branch_exists=AsyncMock(return_value=True),
        create_pull_request=create_pr,
    ))
    await sign_in(authorized, "user-123")
    url = f"/api/work-items/{item['id']}"
    if transport == "slack":
        approved = await slack_request(authorized, f"approve {item['id']}")
    else:
        approved = await authorized.post(f"{url}/approvals", json={
            "kind": "pull_request", "decision": "approve",
        })
    assert approved.status_code == 200
    assert (await authorized.get(url)).json()["status"] == "completed"
    assert (await authorized.get(f"{url}/audit-log")).status_code == 403
    await sign_in(authorized, "admin", organization="other")
    assert (await authorized.get(f"{url}/audit-log")).status_code == 404
    await sign_in(authorized, "admin")
    response = await authorized.get(f"{url}/audit-log")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    source, started, completed = response.json()
    assert source["actor_subject"] == "user-123"
    assert source["identity_provider"] == "https://identity.example"
    assert source["repository_role"] == source["effective_role"] == "approver"
    assert source["transport"] == transport
    for row in (started, completed):
        assert row["actor_subject"] == "delivery:github"
        assert row["identity_provider"] == "urn:kelpie:service"
        assert row["details"]["approval_audit_id"] == source["id"]
        assert row["transport"] == "background"
        assert row["actor_id"] is row["effective_role"] is row["source_ip"] is None
    assert completed["details"]["pull_request_number"] == 9
    create_pr.assert_awaited_once()
