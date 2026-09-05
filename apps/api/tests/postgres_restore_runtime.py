"""Real loopback API against a restored database, using a SELECT-only PostgreSQL login."""

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from postgres_restore import fingerprint, restore_drill
from postgres_restore_seed import ISSUER, RestoreSeed, seed_database
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class RestoredAPI:
    api_url: str
    cookie_name: str
    seed: RestoreSeed
    client: httpx.Client = field(repr=False)
    log_path: Path


def reader_url(drill, database, role):
    assert role in drill.roles and database in drill.databases
    password = secrets.token_hex(32)

    async def enable():
        engine = create_async_engine(drill.database_url(database))
        try:
            async with engine.begin() as connection:
                # Generated hexadecimal only, never user input or a retained credential.
                await connection.exec_driver_sql(
                    f'ALTER ROLE "{role}" LOGIN PASSWORD \'{password}\'',
                )
        except Exception:
            raise AssertionError("isolated reader login setup failed (output withheld)") from None
        finally:
            await engine.dispose()

    asyncio.run(enable())
    return drill.url.set(database=database, username=role, password=password).render_as_string(
        hide_password=False,
    )


@contextmanager
def restored_api(directory: Path, *, port=None):
    with restore_drill(directory) as drill:
        source = drill.create_database()
        drill.migrate(source)
        seed = asyncio.run(seed_database(drill.database_url(source)))
        reader = drill.create_reader(source)
        before = asyncio.run(fingerprint(drill.database_url(source)))
        archive = drill.backup(source)
        target = drill.create_database()
        assert drill.restore(target, archive).returncode == 0, "isolated restore failed"
        assert asyncio.run(fingerprint(drill.database_url(target))) == before
        url = reader_url(drill, target, reader)
        if port is None:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
        api_url = f"http://localhost:{port}"
        cookie_name = f"restore_drill_{uuid.uuid4().hex}"
        environment = {
            "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "apps" / "api"),
            "DATABASE_URL": url, "DATABASE_SCHEMA_MODE": "validate", "AUTH_MODE": "oidc",
            "OIDC_ISSUER_URL": ISSUER, "OIDC_CLIENT_ID": "restore-drill",
            "OIDC_REDIRECT_URI": "https://restore.example.invalid/auth/callback",
            "OIDC_SESSION_COOKIE_NAME": cookie_name,
            "DASHBOARD_URL": "https://restore.example.invalid",
            "ARTIFACT_ROOT": str(directory / "missing-objects"),
        }
        log_path = directory / "api.log"
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        process = None
        try:
            with os.fdopen(descriptor, "wb") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                     "--port", str(port), "--no-access-log"],
                    cwd=directory, env=environment, stdout=log, stderr=subprocess.STDOUT,
                )
            with httpx.Client(base_url=api_url, timeout=2, trust_env=False) as client:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    assert process.poll() is None, "restored API exited (log withheld)"
                    try:
                        if client.get("/readyz").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    raise AssertionError("restored API did not become ready")
                client.cookies.set(cookie_name, seed.token)
                yield RestoredAPI(api_url, cookie_name, seed, client, log_path)
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            log_path.unlink()
        # Startup recovery really runs but cannot acquire write locks or change retained data.
        assert asyncio.run(fingerprint(drill.database_url(target))) == before
        assert asyncio.run(fingerprint(drill.database_url(source))) == before
