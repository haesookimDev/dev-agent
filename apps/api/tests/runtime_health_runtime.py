"""Disposable migrated API fixture: real HTTP observations, synthetic worker/lease rows."""

import asyncio
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
from alembic import command
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_migrations import migration_config

from app.models import (
    Organization,
    Repository,
    ResourceLease,
    WorkerHost,
    WorkerState,
    WorkItem,
    WorkSource,
    WorkStatus,
    utcnow,
)
from app.worker_credentials import issue_credential

ROOT = Path(__file__).resolve().parents[3]


def observed(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    return {(sample.name, tuple(sample.labels.items())): sample.value
            for family in text_string_to_metric_families(response.text)
            for sample in family.samples if sample.name.startswith("kelpie_runtime_")}


def await_observation(client, predicate):
    deadline = time.monotonic() + 15
    while True:
        value = observed(client)
        if predicate(value):
            return value
        assert time.monotonic() < deadline, "runtime observation did not reach expected state"
        time.sleep(0.05)


@contextmanager
def runtime_health_runtime(directory: Path, *, port=None):
    database = directory / "runtime.db"
    database_url = f"sqlite+aiosqlite:///{database}"
    command.upgrade(migration_config(database_url), "head")

    async def seed():
        engine = create_async_engine(database_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                session.add(Organization(id="runtime-acceptance"))
                await session.flush()
                session.add(Repository(name="acceptance/runtime",
                                       organization_id="runtime-acceptance"))
                identity = await issue_credential(session, "runtime-worker", actor="test",
                                                  reason="observability fixture; no VM")
                worker = await session.get(WorkerHost, identity.worker_id)
                worker.state = WorkerState.ONLINE
                worker.last_seen_at = utcnow() - timedelta(minutes=5)
                items = []
                for name, status in [("Queued observation", WorkStatus.QUEUED),
                                     ("Synthetic lease", WorkStatus.IMPLEMENTING),
                                     ("Human wait", WorkStatus.AWAITING_APPROVAL)]:
                    item = WorkItem(
                        title=name, requirement="Verify continuous observation without execution",
                        repository="acceptance/runtime", organization_id="runtime-acceptance",
                        source=WorkSource.WEB, status=status,
                        created_at=utcnow() - timedelta(minutes=20),
                    )
                    session.add(item)
                    await session.flush()
                    items.append(item.id)
                session.add(ResourceLease(
                    work_item_id=items[1], worker_id=worker.id, token_hash="synthetic-lease-hash",
                    state="active", expires_at=utcnow() - timedelta(minutes=5),
                ))
                await session.commit()
                return identity, items[0]
        finally:
            await engine.dispose()

    identity, queued_id = asyncio.run(seed())
    if port is None:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
    api_url = f"http://127.0.0.1:{port}"
    environment = {
        "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "apps/api"),
        "DATABASE_URL": database_url, "DATABASE_SCHEMA_MODE": "validate",
        "AUTH_MODE": "development", "WORKER_AUTH_MODE": "scoped",
        "DEVELOPMENT_ORGANIZATION": "runtime-acceptance",
        "DEVELOPMENT_SUBJECT": "runtime-admin", "ARTIFACT_ROOT": str(directory / "artifacts"),
    }

    def query_failure(enabled):
        # The only renamed table belongs to this disposable fixture. No live VM exists.
        statement = ("ALTER TABLE worker_hosts RENAME TO runtime_private_worker_fixture" if enabled
                     else "ALTER TABLE runtime_private_worker_fixture RENAME TO worker_hosts")
        with sqlite3.connect(database) as connection:
            connection.execute(statement)

    def release_synthetic_lease():
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE resource_leases SET state='released'")

    with (directory / "api.log").open("xb") as log:
        os.chmod(log.name, 0o600)
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                 "--port", str(port), "--no-access-log", "--no-proxy-headers"],
                cwd=directory, env=environment, stdout=log, stderr=subprocess.STDOUT,
            )
            with httpx.Client(base_url=api_url, timeout=1) as client:
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None, "owned API exited before readiness"
                    try:
                        if client.get("/readyz").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, "owned API readiness timed out"
                    time.sleep(0.05)

                def heartbeat():
                    response = client.post(f"/api/workers/{identity.worker_id}/heartbeat", json={
                        "cpu_available": 0, "memory_mb_available": 0,
                        "disk_gb_available": 0, "active_runs": 0,
                    }, headers={"Authorization": f"Bearer {identity.token}"})
                    assert response.status_code == 200

                yield SimpleNamespace(
                    client=client, api_url=api_url, queued_id=queued_id, database=database,
                    query_failure=query_failure, heartbeat=heartbeat,
                    release_synthetic_lease=release_synthetic_lease,
                )
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
