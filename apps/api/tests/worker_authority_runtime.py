"""Real HTTP control-plane authority checks with disposable OIDC identities."""

import asyncio
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import timedelta

import httpx
from artifact_runtime import ISSUER
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import hash_token
from app.models import AuthSession, utcnow


class PrivateCredentials(list):
    def __repr__(self):
        return "<disposable credentials withheld>"


@contextmanager
def admin_client(runtime, index, credentials):
    token = secrets.token_urlsafe(32)
    credentials.append(token)

    async def seed():
        engine = create_async_engine(f"sqlite+aiosqlite:///{runtime.database}")
        try:
            async with async_sessionmaker(engine)() as session:
                # The fixture already registered this administrator and organization.
                session.add(AuthSession(token_hash=hash_token(token), subject=f"admin-{index}",
                    organization=f"artifact-{index}", identity_provider=ISSUER,
                    expires_at=utcnow() + timedelta(minutes=10)))
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(seed())
    with httpx.Client(base_url=runtime.api_url, timeout=3, trust_env=False,
                      headers={"Origin": ISSUER}, cookies={runtime.cookie_name: token}) as client:
        yield client


def snapshot(runtime):
    with sqlite3.connect(runtime.database) as connection:
        return {table: connection.execute(f"SELECT * FROM {table}").fetchall()
                for table in ("work_items", "resource_leases", "agent_events", "approvals",
                              "audit_records", "delivery_jobs")}


def advance(runtime, index, target):
    client, work = runtime.clients[index], runtime.works[index]
    before = client.get(f"/api/work-items/{work}").json()
    response = client.post(f"/api/runs/{work}/transition", headers=runtime.leases[work], json={
        "status": target, "expected_version": before["version"],
    })
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["version"]) == (
        target, before["version"] + 1,
    )
    return response.json()


def reject_resume(runtime, index, target="implementing"):
    client, work = runtime.clients[index], runtime.works[index]
    current = client.get(f"/api/work-items/{work}").json()
    before = snapshot(runtime)
    response = client.post(f"/api/runs/{work}/transition", headers=runtime.leases[work], json={
        "status": target, "expected_version": current["version"],
        "message": "Human approval claimed by an untrusted caller",
        "payload": {"approved": True, "actor": "administrator", "minutes": 1440},
    })
    assert response.status_code == 403
    assert response.json() == {"detail": "worker cannot perform this transition"}
    assert snapshot(runtime) == before


def exercise_authority(runtime, credentials):
    own, foreign = runtime.clients
    first, second = runtime.works
    first_url, second_url = f"/api/work-items/{first}", f"/api/work-items/{second}"
    advance(runtime, 0, "analyzing")
    for waiting in ("awaiting_input", "awaiting_feedback", "awaiting_approval"):
        advance(runtime, 0, waiting)
        reject_resume(runtime, 0)
        if waiting != "awaiting_approval":
            response = own.post(f"{first_url}/feedback", json={
                "message": "Operator requests revision",
            })
            assert response.status_code == 200 and response.json()["status"] == "implementing"
            advance(runtime, 0, "verifying")
        else:
            reject_resume(runtime, 0, "committing")
    with admin_client(runtime, 0, credentials) as admin:
        approval = {"kind": "pull_request", "decision": "approve"}
        before = snapshot(runtime)
        assert own.post(f"{first_url}/approvals", json=approval).status_code == 403
        assert foreign.post(f"{first_url}/approvals", json=approval).status_code == 404
        rejected_origin = admin.post(f"{first_url}/approvals", json=approval,
            headers={"Origin": "https://different.example.invalid"})
        assert rejected_origin.status_code == 403
        assert snapshot(runtime) == before
        events = own.get(f"{first_url}/event-log").json()
        with own.stream("GET", f"{first_url}/events?after={events[-1]['id']}") as stream:
            assert stream.status_code == 200
            accepted = admin.post(f"{first_url}/approvals", json=approval)
            assert accepted.status_code == 200 and accepted.json()["status"] == "committing"
            live = (json.loads(line[6:]) for line in stream.iter_lines()
                    if line.startswith("data: "))
            approved_event, transition = next(live), next(live)
            assert approved_event["event_type"] == "approval.decided"
            assert approved_event["source"] == transition["source"] == "admin-0"
            assert transition["payload"] == {"from": "awaiting_approval", "to": "committing"}
        advance(runtime, 0, "pr_created")
        advance(runtime, 0, "completed")
        response = admin.get(f"{first_url}/audit-log")
        assert response.status_code == 200
        audit = response.json()
    assert own.get(f"{first_url}/audit-log").status_code == 403
    assert [record["action"] for record in audit] == [
        "feedback.created", "feedback.created", "approval.decided",
    ]
    assert audit[-1]["actor_subject"] == "admin-0"
    assert audit[-1]["details"]["delivery_queued"] is False
    advance(runtime, 1, "analyzing")
    exhausted = advance(runtime, 1, "budget_exhausted")
    reject_resume(runtime, 1)
    with admin_client(runtime, 1, credentials) as admin:
        body = {"kind": "budget", "decision": "approve", "payload": {"minutes": 45}}
        before = snapshot(runtime)
        assert foreign.post(f"{second_url}/approvals", json=body).status_code == 403
        assert own.post(f"{second_url}/approvals", json=body).status_code == 404
        assert snapshot(runtime) == before
        accepted = admin.post(f"{second_url}/approvals", json=body)
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "implementing"
        assert accepted.json()["budget_minutes"] == exhausted["budget_minutes"] + 45
        advance(runtime, 1, "verifying")
        response = admin.get(f"{second_url}/audit-log")
        assert response.status_code == 200
        assert response.json()[-1]["actor_subject"] == "admin-1"
    assert foreign.get(f"{second_url}/audit-log").status_code == 403
    return {"mock_status": own.get(first_url).json()["status"],
            "budget_status": foreign.get(second_url).json()["status"]}
