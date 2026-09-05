import base64
import hashlib
import hmac
import time
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings, get_settings
from app.integrations.github import GitHubAppClient
from app.integrations.slack import SlackNotifier
from app.main import app
from app.models import WorkItem, WorkStatus
from app.oidc import get_oidc_provider
from app.secrets import SecretUnavailableError


def configure_file(tmp_path, name, **extra):
    source = tmp_path / name
    source.write_text("synthetic-first-secret-32-characters")
    settings = Settings(_env_file=None, **{f"{name}_file": str(source)}, **extra)
    return source, settings


async def test_worker_auth_reads_rotated_file_and_fails_closed(client, tmp_path):
    source, settings = configure_file(tmp_path, "worker_shared_secret",
                                       worker_auth_mode="development")
    app.dependency_overrides[get_settings] = lambda: settings
    payload = {"name": "file-worker", "cpu_total": 2, "memory_mb_total": 4096,
               "disk_gb_available": 30, "labels": {}}

    async def register(token):
        return await client.post("/api/workers/register", json=payload,
                                 headers={"Authorization": f"Bearer {token}"})

    old = source.read_text()
    assert (await register(old)).status_code == 200
    source.write_text("synthetic-rotated-secret-32-characters")
    assert (await register(old)).status_code == 401
    assert (await register(source.read_text())).status_code == 200
    assert (await client.post("/api/workers/register", json=payload,
                              headers={"Authorization":
                                       "Bearer development-worker-secret-change-me"}
                              )).status_code == 401
    source.unlink()
    response = await register(old)
    assert response.status_code == 503
    assert response.json() == {"detail": "configured secret is unavailable"}
    assert response.headers["cache-control"] == "no-store"
    assert str(source) not in response.text
    assert old not in response.text


@pytest.mark.parametrize("kind", ["github", "slack"])
async def test_webhook_signatures_use_current_file_before_parsing(client, tmp_path, kind):
    name = "github_webhook_secret" if kind == "github" else "slack_signing_secret"
    source, settings = configure_file(tmp_path, name)
    app.dependency_overrides[get_settings] = lambda: settings

    async def send(secret):
        if kind == "github":
            body = b"{}"
            signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            return await client.post("/webhooks/github", content=body, headers={
                "X-GitHub-Event": "ping", "X-GitHub-Delivery": str(time.time_ns()),
                "X-Hub-Signature-256": "sha256=" + signature,
            })
        timestamp = str(int(time.time()))
        body = b"text=help"
        signature = hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body,
                             hashlib.sha256).hexdigest()
        return await client.post("/webhooks/slack/commands", content=body, headers={
            "X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": "v0=" + signature,
        })

    old = source.read_text()
    expected = 202 if kind == "github" else 200
    assert (await send(old)).status_code == expected
    source.write_text("synthetic-rotated-secret")
    assert (await send(old)).status_code == 401
    assert (await send(source.read_text())).status_code == expected
    source.write_text("")
    assert (await send(old)).status_code == 503


def mock_http(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
async def test_cached_oidc_provider_uses_rotated_file(monkeypatch, tmp_path, method):
    source, settings = configure_file(tmp_path, "oidc_client_secret",
        auth_mode="oidc", oidc_issuer_url="https://identity.example",
        oidc_client_id="kelpie", oidc_redirect_uri="https://control.example/auth/callback",
        dashboard_url="https://dashboard.example")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id_token": "synthetic-id-token"})

    mock_http(monkeypatch, handler)
    provider = get_oidc_provider(settings)
    metadata = {"token_endpoint": "https://identity.example/token",
                "token_endpoint_auth_methods_supported": [method, "none"]}
    for value in ("synthetic-first-secret", "synthetic-rotated-secret"):
        source.write_text(value)
        assert get_oidc_provider(settings) is provider
        await provider._exchange_code(metadata, "code", "verifier")
        request = requests[-1]
        if method == "client_secret_basic":
            assert request.headers["authorization"] == "Basic " + base64.b64encode(
                f"kelpie:{value}".encode()).decode()
        else:
            assert parse_qs(request.content.decode())["client_secret"] == [value]
    source.unlink()
    # A missing confidential-client file must not downgrade to public-client auth.
    with pytest.raises(SecretUnavailableError):
        await provider._exchange_code(metadata, "code", "verifier")
    assert len(requests) == 2


async def test_slack_status_and_upload_use_rotated_token(monkeypatch, tmp_path):
    source, settings = configure_file(tmp_path, "slack_bot_token", slack_channel_id="C-test")
    expected = source.read_text()
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "slack.com":
            assert request.headers["authorization"] == f"Bearer {expected}"
        return httpx.Response(200, json={"ok": True, "file_id": "file-test",
                                       "upload_url": "https://upload.example/file"})

    mock_http(monkeypatch, handler)
    notifier = SlackNotifier(settings)
    work = WorkItem(id="work-test", title="Synthetic work", status=WorkStatus.VERIFYING,
                    correlation_id="correlation-test")
    await notifier.post_status(work)
    expected = "synthetic-rotated-token"
    source.write_text(expected)
    image = tmp_path / "image.png"
    image.write_bytes(b"synthetic-upload-content")
    await notifier.upload_image(image, "Synthetic image")
    assert len(calls) == 4
    source.unlink()
    with pytest.raises(SecretUnavailableError):
        await notifier.post_status(work)
    assert len(calls) == 4


async def test_github_signing_key_rotation_and_safe_file_failure(tmp_path):
    source = tmp_path / "private-key.pem"
    github = GitHubAppClient(Settings(_env_file=None, github_app_id=123,
                                     github_private_key_path=str(source)))
    for _ in range(2):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        source.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        token = await github.app_jwt()
        assert jwt.decode(token, key.public_key(), algorithms=["RS256"])["iss"] == "123"
    source.unlink()
    with pytest.raises(SecretUnavailableError, match="configured secret is unavailable"):
        await github.app_jwt()
