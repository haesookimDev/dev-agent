"""Exercise file rotation over HTTP against a real, isolated API process."""

import hashlib
import hmac
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


def test_running_api_observes_secret_rotation_and_recovery(tmp_path):
    root = Path(__file__).resolve().parents[3]
    source = tmp_path / "secret"
    original, rotated = secrets.token_hex(32), secrets.token_hex(32)
    source.write_text(original)
    source.chmod(0o600)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(root / "apps/api"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}",
        "DATABASE_SCHEMA_MODE": "validate",
        "AUTH_MODE": "development",
        "DEVELOPMENT_ORGANIZATION": "secret-runtime-test",
        "ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "WORKER_SHARED_SECRET_FILE": str(source),
        "GITHUB_WEBHOOK_SECRET_FILE": str(source),
        "GITHUB_APP_ID": "",
        "GITHUB_PRIVATE_KEY_PATH": "",
        "SLACK_BOT_TOKEN": "",
        "SLACK_CHANNEL_ID": "",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "",
    }
    # Run outside the checkout so a developer's .env can never enter this process.
    subprocess.run([sys.executable, "-m", "alembic", "-c", str(root / "apps/api/alembic.ini"),
                    "upgrade", "head"], cwd=tmp_path, env=environment, check=True,
                   capture_output=True, timeout=30)
    log_path = tmp_path / "api.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--no-access-log"], cwd=tmp_path, env=environment,
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2) as client:
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None, "isolated API exited during startup"
                    try:
                        if client.get("/readyz").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, "isolated API readiness timed out"
                    time.sleep(0.05)

                def register(token):
                    return client.post("/api/workers/register", headers={
                        "Authorization": f"Bearer {token}",
                    }, json={"name": "runtime-worker", "cpu_total": 2,
                             "memory_mb_total": 4096, "disk_gb_available": 30, "labels": {}})

                assert register(original).status_code == 200
                replacement = tmp_path / "next-secret"
                replacement.write_text(rotated)
                replacement.chmod(0o600)
                replacement.replace(source)
                assert register(original).status_code == 401
                assert register(rotated).status_code == 200
                signature = "sha256=" + hmac.new(rotated.encode(), b"{}",
                                                  hashlib.sha256).hexdigest()
                assert client.post("/webhooks/github", content=b"{}", headers={
                    "X-GitHub-Event": "ping", "X-GitHub-Delivery": "runtime-rotation",
                    "X-Hub-Signature-256": signature,
                }).status_code == 202
                source.unlink()
                response = register(rotated)
                assert response.status_code == 503
                assert response.json() == {"detail": "configured secret is unavailable"}
                assert response.headers["cache-control"] == "no-store"
                source.mkdir()
                for _ in range(3):
                    assert register(rotated).status_code == 503
                source.rmdir()
                source.write_text(rotated)
                source.chmod(0o600)
                assert register(rotated).status_code == 200
                assert process.poll() is None
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    for path in (log_path, tmp_path / "runtime.db"):
        retained = path.read_bytes()
        assert original.encode() not in retained
        assert rotated.encode() not in retained
