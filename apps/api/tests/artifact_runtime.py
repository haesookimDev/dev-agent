"""Disposable real-HTTP artifact drill; synthetic OIDC sessions, no IdP, SCM or VM."""

import asyncio
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import hash_token
from app.iam import OrganizationPolicy, apply_policy
from app.models import Artifact, AuthSession, utcnow
from app.worker_credentials import issue_credential

ROOT = Path(__file__).resolve().parents[3]
ISSUER = "https://artifact.example.invalid"
CONTENT = b"Owned artifact acceptance evidence\n"


@dataclass(frozen=True)
class ArtifactRuntime:
    api_url: str
    cookie_name: str
    database: Path
    root: Path
    works: tuple[str, str]
    artifacts: tuple[str, str]
    clients: tuple[httpx.Client, httpx.Client] = field(repr=False)
    tokens: tuple[str, str] = field(repr=False)
    leases: dict[str, dict[str, str]] = field(repr=False)

    def key(self, artifact_id):
        with sqlite3.connect(self.database) as connection:
            row = connection.execute("SELECT object_key FROM artifacts WHERE id = ?",
                                     (artifact_id,)).fetchone()
        assert row is not None
        return row[0]

    def retain_alias(self, key, name="Retained unavailable evidence.txt"):
        async def insert():
            engine = create_async_engine(f"sqlite+aiosqlite:///{self.database}")
            try:
                async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                    row = Artifact(work_item_id=self.works[0], kind="evidence", name=name,
                                   content_type="text/plain", object_key=key,
                                   size_bytes=len(CONTENT))
                    session.add(row)
                    await session.commit()
                    return row.id
            finally:
                await engine.dispose()
        return asyncio.run(insert())


@contextmanager
def artifact_runtime(directory: Path, *, port=None, web_origin="http://localhost:3000",
                     app_target="app.main:app", verify_log=None):
    database = directory / "artifact.db"
    database_url = f"sqlite+aiosqlite:///{database}"
    migration = Config(ROOT / "apps/api/alembic.ini")
    migration.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(migration, "head")
    tokens = (secrets.token_urlsafe(32), secrets.token_urlsafe(32))

    async def seed():
        engine = create_async_engine(database_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                for index, token in enumerate(tokens):
                    organization = f"artifact-{index}"
                    subject = f"operator-{index}"
                    await apply_policy(session, OrganizationPolicy.model_validate({
                        "organization_id": organization, "issuer": ISSUER,
                        "claim": organization,
                        "members": [{"subject": f"admin-{index}", "role": "administrator"},
                                    {"subject": subject, "role": "operator"}],
                        "repositories": [{"name": f"acceptance/{organization}"}],
                    }))
                    session.add(AuthSession(token_hash=hash_token(token), subject=subject,
                        organization=organization, identity_provider=ISSUER,
                        expires_at=utcnow() + timedelta(hours=1)))
                identity = await issue_credential(session, "artifact-worker", actor="drill",
                    reason="Synthetic artifact isolation; no VM execution")
                await session.commit()
                return identity
        finally:
            await engine.dispose()

    identity = asyncio.run(seed())
    if port is None:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
    api_url = f"http://localhost:{port}"
    cookie_name = f"artifact_drill_{uuid.uuid4().hex}"
    root = directory / "objects"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(str(ROOT / part) for part in ("apps/api", "apps/api/tests")),
        "DATABASE_URL": database_url, "DATABASE_SCHEMA_MODE": "validate", "AUTH_MODE": "oidc",
        "WORKER_AUTH_MODE": "scoped", "OIDC_ISSUER_URL": ISSUER,
        "OIDC_CLIENT_ID": "artifact-drill", "OIDC_REDIRECT_URI": f"{ISSUER}/auth/callback",
        "DASHBOARD_URL": ISSUER, "OIDC_SESSION_COOKIE_NAME": cookie_name,
        "CORS_ORIGINS": web_origin, "ARTIFACT_ROOT": str(root), "LEASE_SECONDS": "3600",
    }
    log_path = directory / "api.log"
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    process = None
    try:
        with os.fdopen(descriptor, "wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", app_target, "--host", "127.0.0.1",
                 "--port", str(port), "--no-access-log", "--no-proxy-headers"],
                cwd=directory, env=environment, stdout=log, stderr=subprocess.STDOUT,
            )
        with (httpx.Client(base_url=api_url, timeout=2, trust_env=False) as own,
              httpx.Client(base_url=api_url, timeout=2, trust_env=False) as foreign):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert process.poll() is None, "owned artifact API exited (log withheld)"
                try:
                    if own.get("/readyz").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("owned artifact API readiness timed out")
            works = []
            for index, client in enumerate((own, foreign)):
                client.cookies.set(cookie_name, tokens[index])
                client.headers["Origin"] = ISSUER
                response = client.post("/api/work-items", json={
                    "title": "산출물 격리 검증 / Artifact isolation" if index == 0
                             else "다른 조직의 작업 / Other organization",
                    "requirement": "Verify owned artifact visibility with synthetic evidence",
                    "repository": f"acceptance/artifact-{index}",
                })
                assert response.status_code == 201
                works.append(response.json()["id"])
            headers = {"Authorization": f"Bearer {identity.token}"}
            response = own.post("/api/workers/register", headers=headers, json={
                "name": "artifact-worker", "cpu_total": 8, "memory_mb_total": 16384,
                "disk_gb_available": 500, "labels": {"virtualization": "mock"},
            })
            assert response.status_code == 200
            leases = {}
            for _ in works:
                response = own.post(f"/api/workers/{identity.worker_id}/claim", headers=headers,
                                   json={"cpu": 2, "memory_mb": 4096, "disk_gb": 30})
                assert response.status_code == 200
                claim = response.json()
                leases[claim["work_item"]["id"]] = {"X-Kelpie-Lease": claim["lease_token"]}
            artifacts = []
            for index, work in enumerate(works):
                response = own.post(f"/api/runs/{work}/artifacts/upload", headers=leases[work],
                    params={"name": "owned-evidence.txt", "content_type": "text/plain"},
                    content=CONTENT if index == 0 else b"Foreign synthetic artifact\n")
                assert response.status_code == 201
                artifacts.append(response.json()["id"])
            yield ArtifactRuntime(api_url, cookie_name, database, root, tuple(works),
                                  tuple(artifacts), (own, foreign), tokens, leases)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        try:
            if verify_log is not None:
                verify_log(log_path.read_text())
        finally:
            log_path.unlink()
