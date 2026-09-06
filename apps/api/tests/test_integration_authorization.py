import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

import pytest
from sqlalchemy import delete, select
from test_authorization import authorized as authorized
from test_authorization import create_item, database
from test_oidc import oidc_settings

from app.config import get_settings
from app.main import app
from app.models import (
    Approval,
    Feedback,
    Membership,
    Principal,
    SlackIdentity,
    WebhookDelivery,
    WorkItem,
    WorkStatus,
)


async def github_request(client, repository="acme/service", installation=12, delivery="delivery-1"):
    secret = secrets.token_urlsafe(32)
    config = oidc_settings().model_copy(update={"github_webhook_secret": secret})
    app.dependency_overrides[get_settings] = lambda: config
    body = json.dumps({
        "action": "labeled", "installation": {"id": installation},
        "repository": {"full_name": repository},
        "issue": {"number": 42, "title": "Webhook work", "body": "Test work creation",
                  "labels": [{"name": "agent-ready"}], "user": {"login": "issue-author"}},
    }).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return await client.post("/webhooks/github", content=body, headers={
        "X-GitHub-Event": "issues", "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": signature,
    })


async def test_github_registered_installation_routes_to_organization_and_deduplicates(authorized):
    assert (await github_request(authorized, repository="ACME/Service")).status_code == 202
    assert (await github_request(authorized)).status_code == 200
    async with database() as session:
        works = (await session.scalars(select(WorkItem))).all()
        assert len(works) == 1
        assert works[0].organization_id == "acme"
        assert works[0].repository == "acme/service"
        assert works[0].github_installation_id == 12


@pytest.mark.parametrize("repository,installation", [
    ("unregistered/service", 12), ("acme/service", 99), ("acme/service", None),
    ("acme/second", 12), ("acme/service", "12"),
])
async def test_github_rejects_unregistered_repository_installation(
    authorized, repository, installation,
):
    response = await github_request(authorized, repository, installation)
    assert response.status_code == 403
    async with database() as session:
        assert not (await session.scalars(select(WorkItem))).all()
        assert not (await session.scalars(select(WebhookDelivery))).all()


async def slack_request(client, text, team="T-acme", user="U1"):
    secret = secrets.token_urlsafe(32)
    config = oidc_settings().model_copy(update={
        "slack_signing_secret": secret, "slack_approver_user_ids": [user],
        "artifact_root": app.dependency_overrides[get_settings]().artifact_root,
    })
    app.dependency_overrides[get_settings] = lambda: config
    body = urlencode({"text": text, "team_id": team, "user_id": user}).encode()
    timestamp = str(int(time.time()))
    signature = "v0=" + hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return await client.post("/webhooks/slack/commands", content=body, headers={
        "X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": signature,
    })


async def test_slack_repository_grant_allows_feedback_and_approval(authorized, monkeypatch):
    item = await create_item(authorized)
    feedback = await slack_request(authorized, f"feedback {item['id']} fix this")
    assert feedback.status_code == 200
    async with database() as session:
        work = await session.get(WorkItem, item["id"])
        work.status = WorkStatus.AWAITING_APPROVAL
        await session.commit()

    async def delivery_ready(*_):
        return False

    monkeypatch.setattr("app.main.validate_delivery_ready", delivery_ready)
    approval = await slack_request(authorized, f"approve {item['id']}")
    assert approval.status_code == 200
    async with database() as session:
        linked = await session.get(SlackIdentity, ("T-acme", "U1"))
        assert (await session.scalar(select(Feedback))).actor == linked.principal_id
        assert (await session.scalar(select(Approval))).actor == linked.principal_id
        assert (await session.get(WorkItem, item["id"])).status == WorkStatus.COMMITTING


@pytest.mark.parametrize("team,user,repository,expected", [
    ("T-fake", "U1", "acme/service", 403), ("T-acme", "U-fake", "acme/service", 403),
    ("T-other", "U1", "acme/service", 404), ("T-acme", "U1", "acme/second", 403),
])
async def test_slack_cannot_bypass_repository_authorization(
    authorized, team, user, repository, expected,
):
    item = await create_item(authorized, repository)
    for action in ("approve", "feedback"):
        text = f"{action} {item['id']}" + (" message" if action == "feedback" else "")
        response = await slack_request(authorized, text, team, user)
        assert response.status_code == expected
    async with database() as session:
        assert not (await session.scalars(select(Feedback))).all()
        assert not (await session.scalars(select(Approval))).all()


async def test_revoked_member_cannot_use_existing_slack_link(authorized):
    item = await create_item(authorized)
    async with database() as session:
        principal = await session.scalar(select(Principal).where(Principal.subject == "user-123"))
        await session.execute(delete(Membership).where(
            Membership.organization_id == "acme", Membership.principal_id == principal.id,
        ))
        await session.commit()
    response = await slack_request(authorized, f"approve {item['id']}")
    assert response.status_code == 403
