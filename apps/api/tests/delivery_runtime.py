"""Loopback SCM + real Git/API fixture; never contacts GitHub or executes a VM."""

import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import jwt
from alembic import command
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_migrations import migration_config

from app.models import DeliveryBundle, Organization, Repository, WorkItem, WorkSource, WorkStatus
from app.worker_credentials import issue_credential

ROOT = Path(__file__).resolve().parents[3]


def free_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


@contextmanager
def delivery_runtime(directory: Path, *, port=None, web_origin="http://127.0.0.1:13460",
                     replace_bundle_on_token=False):
    database_url = f"sqlite+aiosqlite:///{directory / 'delivery.db'}"
    command.upgrade(migration_config(database_url), "head")
    bare, source = directory / "remote.git", directory / "source"
    git_env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
               "GIT_ALLOW_PROTOCOL": "file"}

    def git(*arguments, cwd=directory):
        result = subprocess.run(["git", *arguments], cwd=cwd, env=git_env,
                                capture_output=True, timeout=10)
        assert result.returncode == 0, "isolated Git fixture command failed"
        return result.stdout.decode()

    git("init", "--bare", "--initial-branch=main", str(bare))
    git("init", "--initial-branch=main", str(source))
    (source / "README.md").write_text("Before approval\n")
    git("add", "README.md", cwd=source)
    git("-c", "user.name=Acceptance", "-c", "user.email=test@example.invalid",
        "commit", "-m", "Synthetic base", cwd=source)
    git("remote", "add", "origin", str(bare), cwd=source)
    git("push", "origin", "main", cwd=source)
    (source / "README.md").write_text("Approved delivery\n")
    patch = git("diff", "--binary", cwd=source).encode()
    patch_path = directory / "artifacts" / "approved.patch"
    patch_path.parent.mkdir()
    patch_path.write_bytes(patch)

    async def seed():
        engine = create_async_engine(database_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                session.add(Organization(id="delivery-acceptance"))
                await session.flush()
                identity = await issue_credential(session, "acceptance-worker", actor="test",
                                                  reason="protocol fixture, no VM")
                ids = []
                for name, repository in [("Delivery acceptance", "acceptance/service"),
                                         ("Delivery failure", "acceptance/failure")]:
                    session.add(Repository(name=repository, organization_id="delivery-acceptance",
                                           github_installation_id=1))
                    item = WorkItem(
                        source=WorkSource.WEB, organization_id="delivery-acceptance",
                        title=name, requirement="Verify approval-linked service execution",
                        repository=repository, status=WorkStatus.AWAITING_APPROVAL,
                        version=10, assigned_worker_id=identity.worker_id,
                        github_installation_id=1,
                    )
                    session.add(item)
                    await session.flush()
                    session.add(DeliveryBundle(work_item_id=item.id, object_path=str(patch_path),
                                                sha256=hashlib.sha256(patch).hexdigest(),
                                                size_bytes=len(patch)))
                    ids.append(item.id)
                await session.commit()
                return ids
        finally:
            await engine.dispose()

    success_id, failure_id = asyncio.run(seed())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = directory / "test-only-key.pem"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    token = secrets.token_urlsafe(32)
    pulls, writes, token_requests = {}, [], []

    class SCM(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, value):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.reply(401, {})
                return False
            return True

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/app/installations/1/access_tokens":
                encoded = self.headers.get("Authorization", "").removeprefix("Bearer ")
                jwt.decode(encoded, key.public_key(), algorithms=["RS256"], issuer="1")
                assert body["permissions"]["contents"] == "write"
                token_requests.append(1)
                if replace_bundle_on_token:
                    patch_path.write_bytes(
                        patch.replace(b"Approved delivery", b"Tampered delivery"),
                    )
                self.reply(201, {"token": token})
            elif self.authorized() and self.path == "/repos/acceptance/service/pulls":
                head = body["head"]
                # A PR fixture is accepted only after a real commit reached the bare remote.
                assert git("show", f"{head}:README.md", cwd=bare) == "Approved delivery\n"
                assert head not in pulls
                pulls[head] = "https://github.com/acceptance/service/pull/42"
                writes.append(head)
                self.reply(201, {"html_url": pulls[head]})
            else:
                self.reply(404, {})

        def do_GET(self):
            if not self.authorized():
                return
            target = urlsplit(self.path)
            if target.path == "/repos/acceptance/failure":
                self.reply(503, {"message": "synthetic-private-upstream-failure"})
            elif target.path == "/repos/acceptance/service":
                self.reply(200, {"default_branch": "main"})
            elif target.path == "/repos/acceptance/service/pulls":
                head = parse_qs(target.query)["head"][0].split(":", 1)[1]
                self.reply(200, [{"html_url": pulls[head]}] if head in pulls else [])
            elif target.path.startswith("/repos/acceptance/service/git/ref/heads/"):
                branch = unquote(target.path.split("/heads/", 1)[1])
                branches = git("for-each-ref", "--format=%(refname:short)", cwd=bare).split()
                exists = branch in branches
                self.reply(200 if exists else 404, {})
            else:
                self.reply(404, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), SCM)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = port or free_port()
    api_url = f"http://127.0.0.1:{port}"
    environment = {
        **git_env, "PYTHONPATH": str(ROOT / "apps/api"),
        "DATABASE_URL": database_url, "DATABASE_SCHEMA_MODE": "validate",
        "AUTH_MODE": "development", "WORKER_AUTH_MODE": "scoped",
        "DEVELOPMENT_ORGANIZATION": "delivery-acceptance",
        "DEVELOPMENT_SUBJECT": "acceptance-admin", "ARTIFACT_ROOT": str(directory / "artifacts"),
        "GITHUB_APP_ID": "1", "GITHUB_PRIVATE_KEY_PATH": str(key_path),
        "GITHUB_API_URL": f"http://127.0.0.1:{server.server_port}",
        "CORS_ORIGINS": web_origin, "DASHBOARD_URL": web_origin,
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{bare.as_uri()}.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/acceptance/service.git",
    }
    with (directory / "api.log").open("wb") as log:
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                 "--port", str(port), "--no-access-log", "--no-proxy-headers"],
                cwd=directory, env=environment, stdout=log, stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 15
            with httpx.Client(base_url=api_url, timeout=1) as client:
                while True:
                    assert process.poll() is None, "owned API exited before readiness"
                    try:
                        if client.get("/readyz").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, "owned API readiness timed out"
                    time.sleep(0.05)
                yield SimpleNamespace(client=client, api_url=api_url, success_id=success_id,
                                      failure_id=failure_id, writes=writes, git=git, bare=bare,
                                      database_url=database_url, patch_path=patch_path,
                                      patch=patch, token_requests=token_requests)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
