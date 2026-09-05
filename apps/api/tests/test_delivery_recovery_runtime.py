"""Exercise deferred recovery in a real API process, with no external SCM writes."""

import asyncio
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
from alembic import command
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_migrations import migration_config

from app.models import DeliveryBundle, DeliveryJob, WorkItem, WorkSource, WorkStatus
from app.worker_credentials import issue_credential
from app.worker_quarantine import quarantine_worker


def test_api_resumes_orphans_after_schema_recovery_without_restarting(tmp_path):
    database_path = tmp_path / "recovery.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    command.upgrade(migration_config(database_url), "head")

    async def seed():
        engine = create_async_engine(database_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                worker = await issue_credential(session, "recovery", actor="test", reason="test")
                blocked = await issue_credential(session, "blocked", actor="test", reason="test")
                for state in ("pending", "retry", "running", "quarantined"):
                    item = WorkItem(
                        source=WorkSource.WEB, title=state, requirement="Isolated recovery test",
                        repository="acme/recovery", status=WorkStatus.COMMITTING,
                        assigned_worker_id=(blocked.worker_id if state == "quarantined"
                                            else worker.worker_id),
                    )
                    session.add(item)
                    await session.flush()
                    session.add_all([
                        DeliveryJob(work_item_id=item.id, state="running" if state == "quarantined"
                                    else state),
                        DeliveryBundle(work_item_id=item.id, object_path="unused-test.patch",
                                       sha256="0" * 64, size_bytes=0),
                    ])
                await session.flush()
                await quarantine_worker(session, blocked.worker_id, actor="test", reason="test")
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(seed())
    with sqlite3.connect(database_path) as database:
        head = database.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        database.execute("UPDATE alembic_version SET version_num = 'test-unready-revision'")

    root = Path(__file__).resolve().parents[3]
    prefix = "kelpie_delivery_startup_recovery_"
    environment = {
        "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root / "apps/api"),
        "DATABASE_URL": database_url, "DATABASE_SCHEMA_MODE": "validate",
        "AUTH_MODE": "development", "WORKER_AUTH_MODE": "scoped",
        "DEVELOPMENT_ORGANIZATION": "recovery-test", "ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "",
    }
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    log_path = tmp_path / "api.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--no-access-log", "--no-proxy-headers"],
            cwd=tmp_path, env=environment, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=1) as client:
                deadline = time.monotonic() + 10
                while True:
                    assert process.poll() is None, "isolated API exited during startup"
                    try:
                        if client.get("/healthz").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                response = client.get("/readyz")
                assert response.status_code == 503
                assert response.json()["database_schema"] == "outdated"
                metrics = client.get("/metrics")
                assert metrics.status_code == 200
                assert (f'{prefix}state{{{prefix}state="waiting_for_database"}} 1.0'
                        in metrics.text)
                with sqlite3.connect(database_path) as database:
                    attempts = database.execute(
                        "SELECT sum(attempts) FROM delivery_jobs",
                    ).fetchone()[0]
                    assert attempts == 0
                    database.execute("UPDATE alembic_version SET version_num = ?", (head,))
                assert client.get("/readyz").status_code == 200
                deadline = time.monotonic() + 10
                while True:
                    assert process.poll() is None
                    assert client.get("/healthz").status_code == 200
                    with sqlite3.connect(database_path) as database:
                        rows = database.execute(
                            "SELECT state, attempts, error FROM delivery_jobs",
                        ).fetchall()
                    metrics = client.get("/metrics")
                    assert metrics.status_code == 200
                    completed = f'{prefix}state{{{prefix}state="completed"}} 1.0' in metrics.text
                    if sum(row[0] == "failed" for row in rows) == 3 and completed:
                        break
                    assert time.monotonic() < deadline, "startup delivery recovery did not retry"
                    time.sleep(0.05)
                assert sorted((row[0], row[1]) for row in rows) == [
                    ("failed", 1), ("failed", 1), ("failed", 1), ("quarantined", 0),
                ]
                assert all(row[2] == "GitHub delivery failed at configuration (internal_error)"
                           for row in rows if row[0] == "failed")
                assert f'{prefix}checks_total{{outcome="completed"}} 1.0' in metrics.text
                assert f'{prefix}checks_total{{outcome="error"}} 0.0' in metrics.text
                assert 'kelpie_delivery_outcomes_total{outcome="failed"} 3.0' in metrics.text
                assert "acme/recovery" not in metrics.text
                assert "unused-test.patch" not in metrics.text
                frozen = next(line for line in metrics.text.splitlines()
                              if line.startswith(prefix + "duration_seconds "))
                assert float(frozen.split()[1]) > 0
                time.sleep(0.02)
                assert frozen in client.get("/metrics").text
                with sqlite3.connect(database_path) as database:
                    assert database.execute(
                        "SELECT count(*) FROM agent_events WHERE event_type = 'delivery.failed'",
                    ).fetchone()[0] == 3
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    # Uvicorn re-raises the received signal after its graceful shutdown handlers finish.
    assert process.returncode in (0, -signal.SIGTERM)
    output = log_path.read_text()
    assert "Application shutdown complete." in output
    assert "Task exception was never retrieved" not in output
