import hashlib
import hmac
import json
import uuid

import pytest
from httpx import AsyncClient

from app.db import SchemaReadiness, SchemaState, get_schema_readiness
from app.main import app


async def create_work(client: AsyncClient, title: str = "Implement health endpoint") -> dict:
    response = await client.post(
        "/api/work-items",
        json={
            "title": title,
            "requirement": "Implement and test a health endpoint",
            "repository": "acme/service",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def register_worker(
    client: AsyncClient, headers: dict[str, str], virtualization: str = "mock"
) -> dict:
    response = await client.post(
        "/api/workers/register",
        headers=headers,
        json={
            "name": "worker-one",
            "cpu_total": 8,
            "memory_mb_total": 16384,
            "disk_gb_available": 500,
            "labels": {"virtualization": virtualization},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_readiness_rejects_schema_mismatch(client: AsyncClient) -> None:
    async def outdated_schema() -> SchemaReadiness:
        return SchemaReadiness(
            state=SchemaState.OUTDATED,
            current_heads=("old-revision",),
            expected_heads=("new-revision",),
        )

    app.dependency_overrides[get_schema_readiness] = outdated_schema
    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database_schema": "outdated",
    }


@pytest.mark.asyncio
async def test_readiness_accepts_current_schema(client: AsyncClient) -> None:
    async def current_schema() -> SchemaReadiness:
        return SchemaReadiness(
            state=SchemaState.CURRENT,
            current_heads=("current-revision",),
            expected_heads=("current-revision",),
        )

    app.dependency_overrides[get_schema_readiness] = current_schema
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database_schema": "current",
    }


@pytest.mark.asyncio
async def test_direct_request_is_queued_and_visible(client: AsyncClient) -> None:
    correlation_id = "22222222-2222-4222-8222-222222222222"
    response = await client.post(
        "/api/work-items",
        headers={"X-Kelpie-Correlation-ID": correlation_id},
        json={
            "title": "Implement health endpoint",
            "requirement": "Implement and test a health endpoint",
            "repository": "acme/service",
        },
    )
    assert response.status_code == 201
    work = response.json()
    assert work["status"] == "queued"
    assert work["version"] == 1
    assert work["correlation_id"] == correlation_id
    assert response.headers["X-Kelpie-Correlation-ID"] == correlation_id
    assert uuid.UUID(response.headers["X-Request-ID"])

    listed = await client.get("/api/work-items")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [work["id"]]

    events = await client.get(f"/api/work-items/{work['id']}/event-log")
    assert events.status_code == 200
    assert events.json()[0]["event_type"] == "work.created"
    assert events.json()[0]["correlation_id"] == correlation_id


@pytest.mark.asyncio
async def test_invalid_correlation_headers_are_replaced(client: AsyncClient) -> None:
    response = await client.get(
        "/healthz",
        headers={"X-Request-ID": "invalid", "X-Kelpie-Correlation-ID": "../../secret"},
    )

    assert response.status_code == 200
    assert uuid.UUID(response.headers["X-Request-ID"])
    assert uuid.UUID(response.headers["X-Kelpie-Correlation-ID"])
    assert response.headers["X-Kelpie-Correlation-ID"] == response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_metrics_expose_low_cardinality_http_and_work_signals(
    client: AsyncClient,
    worker_headers: dict[str, str],
) -> None:
    await client.get("/healthz")
    unmatched_path = "/unmatched/tenant-private-value"
    await client.get(unmatched_path)
    await create_work(client, title="Measure queue latency")
    worker = await register_worker(client, worker_headers)
    await client.post(
        f"/api/workers/{worker['id']}/claim",
        headers=worker_headers,
        json={"cpu": 2, "memory_mb": 4096, "disk_gb": 30},
    )

    response = await client.get("/metrics")

    assert response.status_code == 200
    assert 'kelpie_http_requests_total{method="GET",route="/healthz",status="200"}' in response.text
    assert 'kelpie_http_requests_total{method="GET",route="unmatched",status="404"}' in (
        response.text
    )
    assert 'kelpie_work_claims_total{outcome="claimed"}' in response.text
    assert "kelpie_work_queue_wait_seconds_count" in response.text
    assert 'kelpie_work_transitions_total{from_status="queued",to_status="provisioning"}' in (
        response.text
    )
    assert unmatched_path not in response.text


@pytest.mark.asyncio
async def test_worker_claim_and_approval_gate(
    client: AsyncClient, worker_headers: dict[str, str]
) -> None:
    work = await create_work(client)
    worker = await register_worker(client, worker_headers)
    claim = await client.post(
        f"/api/workers/{worker['id']}/claim",
        headers=worker_headers,
        json={"cpu": 2, "memory_mb": 4096, "disk_gb": 30},
    )
    assert claim.status_code == 200, claim.text
    assignment = claim.json()
    assert assignment["work_item"]["status"] == "provisioning"
    assert assignment["work_item"]["correlation_id"] == work["correlation_id"]
    lease_headers = {"X-Kelpie-Lease": assignment["lease_token"]}

    current = assignment["work_item"]
    for target in ["analyzing", "implementing", "verifying", "awaiting_approval"]:
        changed = await client.post(
            f"/api/runs/{work['id']}/transition",
            headers=lease_headers,
            json={"status": target, "expected_version": current["version"]},
        )
        assert changed.status_code == 200, changed.text
        current = changed.json()

    stale = await client.post(
        f"/api/runs/{work['id']}/transition",
        headers=lease_headers,
        json={"status": "committing", "expected_version": current["version"] - 1},
    )
    assert stale.status_code == 409

    approved = await client.post(
        f"/api/work-items/{work['id']}/approvals",
        headers={"X-Kelpie-User": "release-manager", "X-Kelpie-Role": "approver"},
        json={"kind": "pull_request", "decision": "approve", "payload": {}},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "committing"
    metrics = await client.get("/metrics")
    assert 'kelpie_approvals_total{decision="approve",kind="pull_request"}' in metrics.text


@pytest.mark.asyncio
async def test_verified_bundle_is_stored_and_real_delivery_requires_github_app(
    client: AsyncClient, worker_headers: dict[str, str]
) -> None:
    work = await create_work(client)
    worker = await register_worker(client, worker_headers, virtualization="kvm")
    claim = await client.post(
        f"/api/workers/{worker['id']}/claim",
        headers=worker_headers,
        json={"cpu": 2, "memory_mb": 4096, "disk_gb": 30},
    )
    assignment = claim.json()
    lease_headers = {"X-Kelpie-Lease": assignment["lease_token"]}
    current = assignment["work_item"]
    for target in ["analyzing", "implementing", "verifying"]:
        changed = await client.post(
            f"/api/runs/{work['id']}/transition",
            headers=lease_headers,
            json={"status": target, "expected_version": current["version"]},
        )
        assert changed.status_code == 200, changed.text
        current = changed.json()

    patch = b"diff --git a/a.txt b/a.txt\nnew file mode 100644\n"
    uploaded = await client.post(
        f"/api/runs/{work['id']}/delivery-bundle",
        headers=lease_headers,
        content=patch,
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json() == {
        "sha256": hashlib.sha256(patch).hexdigest(),
        "size_bytes": len(patch),
    }
    downloaded = await client.get(f"/api/work-items/{work['id']}/delivery-bundle")
    assert downloaded.status_code == 200
    assert downloaded.content == patch

    mismatched = await client.post(
        f"/api/runs/{work['id']}/artifacts/upload",
        headers=lease_headers,
        params={"name": "fake.png", "kind": "verification", "content_type": "image/png"},
        content=b"<script>not an image</script>",
    )
    assert mismatched.status_code == 422

    image = b"\x89PNG\r\n\x1a\nverification"
    artifact_upload = await client.post(
        f"/api/runs/{work['id']}/artifacts/upload",
        headers=lease_headers,
        params={"name": "result.png", "kind": "verification", "content_type": "image/png"},
        content=image,
    )
    assert artifact_upload.status_code == 201, artifact_upload.text
    artifact_id = artifact_upload.json()["id"]
    artifacts = await client.get(f"/api/work-items/{work['id']}/artifacts")
    assert artifacts.status_code == 200
    assert artifacts.json()[0]["name"] == "result.png"
    artifact_download = await client.get(
        f"/api/work-items/{work['id']}/artifacts/{artifact_id}"
    )
    assert artifact_download.status_code == 200
    assert artifact_download.content == image

    awaiting = await client.post(
        f"/api/runs/{work['id']}/transition",
        headers=lease_headers,
        json={"status": "awaiting_approval", "expected_version": current["version"]},
    )
    assert awaiting.status_code == 200
    approval = await client.post(
        f"/api/work-items/{work['id']}/approvals",
        headers={"X-Kelpie-User": "release-manager", "X-Kelpie-Role": "approver"},
        json={"kind": "pull_request", "decision": "approve", "payload": {}},
    )
    assert approval.status_code == 409
    assert "GitHub App installation" in approval.text


@pytest.mark.asyncio
async def test_viewer_cannot_approve(client: AsyncClient) -> None:
    work = await create_work(client)
    response = await client.post(
        f"/api/work-items/{work['id']}/approvals",
        headers={"X-Kelpie-User": "observer", "X-Kelpie-Role": "viewer"},
        json={"kind": "pull_request", "decision": "approve", "payload": {}},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_github_webhook_signature_label_and_deduplication(client: AsyncClient) -> None:
    payload = {
        "action": "labeled",
        "repository": {"full_name": "acme/api"},
        "issue": {
            "number": 42,
            "title": "Fix race condition",
            "body": "Reproduce and eliminate the race",
            "user": {"login": "octocat"},
            "labels": [{"name": "agent-ready"}],
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = (
        "sha256=" + hmac.new(b"development-webhook-secret", body, hashlib.sha256).hexdigest()
    )
    headers = {
        "content-type": "application/json",
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": signature,
    }
    first = await client.post("/webhooks/github", headers=headers, content=body)
    assert first.status_code == 202
    duplicate = await client.post("/webhooks/github", headers=headers, content=body)
    assert duplicate.status_code == 200

    works = (await client.get("/api/work-items")).json()
    assert len(works) == 1
    assert works[0]["source"] == "github"
    assert works[0]["source_external_id"] == "github:acme/api:42"


@pytest.mark.asyncio
async def test_invalid_github_signature_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery",
            "X-Hub-Signature-256": "sha256=wrong",
        },
        content=b"{}",
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_preview_registration_and_exclusive_console_lease(
    client: AsyncClient, worker_headers: dict[str, str]
) -> None:
    work = await create_work(client)
    worker = await register_worker(client, worker_headers)
    claim = await client.post(
        f"/api/workers/{worker['id']}/claim",
        headers=worker_headers,
        json={"cpu": 2, "memory_mb": 4096, "disk_gb": 30},
    )
    assignment = claim.json()
    lease_headers = {"X-Kelpie-Lease": assignment["lease_token"]}
    preview = await client.post(
        f"/api/runs/{work['id']}/preview",
        headers=lease_headers,
        json={
            "target_url": "http://10.0.0.2:3000",
            "console_target_url": "http://127.0.0.1:6080",
            "ttl_seconds": 3600,
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["hostname"] == f"{work['id']}.preview.localhost"

    rejected = await client.post(
        f"/api/runs/{work['id']}/preview",
        headers=lease_headers,
        json={"target_url": "http://8.8.8.8:3000", "ttl_seconds": 3600},
    )
    assert rejected.status_code == 422
    assert "allowed VM networks" in rejected.text

    acquired = await client.post(
        f"/api/work-items/{work['id']}/console-lease",
        headers={"X-Kelpie-User": "alice"},
        json={"action": "acquire", "expected_version": 1},
    )
    assert acquired.status_code == 200, acquired.text
    assert acquired.json()["holder"] == "alice"

    conflict = await client.post(
        f"/api/work-items/{work['id']}/console-lease",
        headers={"X-Kelpie-User": "bob"},
        json={"action": "acquire", "expected_version": 2},
    )
    assert conflict.status_code == 409

    resolved = await client.get(
        "/internal/previews/resolve",
        headers=worker_headers,
        params={"host": f"{work['id']}.preview.localhost", "console": "true"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["read_only"] is False
