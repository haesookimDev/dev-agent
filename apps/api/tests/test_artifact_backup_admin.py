import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_artifact_backup import source as source
from test_migrations import migration_config

from app import artifact_backup as backup
from app import artifact_backup_admin as admin
from app import models as m

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def cli(source, tmp_path):
    root, row, content = source
    database = tmp_path / "source.db"
    url = f"sqlite+aiosqlite:///{database}"
    command.upgrade(migration_config(url), "head")
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.insert(m.WorkItem).values(
                id=row.work_item_id, source=m.WorkSource.WEB, title="Synthetic private title",
                requirement="Synthetic requirement", repository="test/backup"))
            values = asdict(row)
            values["id"] = values.pop("artifact_id")
            connection.execute(sa.insert(m.Artifact).values(**values))
    finally:
        engine.dispose()
    dump = tmp_path / "database.dump"
    # Actual SQLite backup exercises CLI hashing; PostgreSQL custom restore is a separate drill.
    with sqlite3.connect(database) as original, sqlite3.connect(dump) as target:
        original.backup(target)
    environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "apps/api"),
                   "DATABASE_URL": url, "ARTIFACT_ROOT": str(root)}

    def run(*args, expected=0, overrides=None):
        result = subprocess.run([sys.executable, "-m", "app.artifact_backup_admin", *args],
            cwd=tmp_path, env=environment | (overrides or {}), capture_output=True,
            text=True, timeout=15)
        assert result.returncode == expected, "artifact CLI returned unexpected status"
        for private in (str(tmp_path), row.object_key, row.name, content.decode(),
                        "Synthetic private title", url):
            assert private not in result.stdout + result.stderr
        if expected:
            assert not result.stdout
            assert "Traceback" not in result.stderr
        return result

    return run, database, dump, url


def test_cli_round_trip_preserves_database_and_existing_destination(cli, source, tmp_path):
    run, database, dump, _ = cli
    root, row, content = source
    before = database.read_bytes()
    snapshot, restored = tmp_path / "snapshot", tmp_path / "restored"
    create_args = ("create", "--database-dump", str(dump), "--output", str(snapshot),
                   "--writers-stopped")
    result = json.loads(run(*create_args).stdout)
    assert result["artifacts"] == result["blobs"] == 1 and result["verified"]
    run(*create_args, expected=2)
    common = ("--database-dump", str(dump), "--manifest-sha256", result["manifest_sha256"])
    assert json.loads(run("verify", *common, "--backup", str(snapshot)).stdout)["verified"]
    assert json.loads(run("restore", *common, "--backup", str(snapshot), "--output",
        str(restored), "--writers-stopped").stdout)["restored"]
    assert json.loads(run("verify-restored", *common,
        overrides={"ARTIFACT_ROOT": str(restored)}).stdout)["verified"]
    assert (restored / row.object_key).read_bytes() == content
    assert (root / row.object_key).read_bytes() == content
    assert database.read_bytes() == before
    run("restore", *common, "--backup", str(snapshot), "--output", str(restored),
        "--writers-stopped", expected=2)
    (restored / row.object_key).write_bytes(b"changed")
    run("verify-restored", *common, overrides={"ARTIFACT_ROOT": str(restored)}, expected=2)
    dump.write_bytes(b"different recovery point")
    run("verify", *common, "--backup", str(snapshot), expected=2)


def test_cli_rejects_missing_acknowledgement_and_unready_schema(cli, tmp_path):
    run, _, dump, _ = cli
    destination = tmp_path / "never-created"
    args = ("create", "--database-dump", str(dump), "--output", str(destination))
    run(*args, expected=2)
    assert not destination.exists()
    outdated = tmp_path / "outdated.db"
    command.upgrade(migration_config(f"sqlite+aiosqlite:///{outdated}"), "20260904_0001")
    run(*args, "--writers-stopped", expected=2,
        overrides={"DATABASE_URL": f"sqlite+aiosqlite:///{outdated}"})
    assert not destination.exists()
    run(*args, "--writers-stopped", expected=2,
        overrides={"DATABASE_URL": "invalid-synthetic-private-dsn"})
    assert not destination.exists()


@pytest.mark.parametrize("kind", ["empty", "missing", "link", "fifo", "directory"])
def test_dump_digest_rejects_non_regular_or_empty_inputs(tmp_path, kind):
    path = tmp_path / "dump"
    if kind == "empty":
        path.touch()
    elif kind == "link":
        outside = tmp_path / "outside"
        outside.write_bytes(b"synthetic dump")
        path.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    with pytest.raises(backup.ArtifactBackupError):
        admin.database_digest(path)


def test_dump_digest_streams_more_than_one_chunk_and_detects_read_changes(tmp_path, monkeypatch):
    path = tmp_path / "dump"
    content = b"synthetic" * (256 * 1024)
    path.write_bytes(content)
    assert admin.database_digest(path) == backup.digest(content)
    original = admin.os.fstat
    calls = 0

    def altered_time(fd):
        nonlocal calls
        calls += 1
        result = original(fd)
        return SimpleNamespace(st_mode=result.st_mode, st_size=result.st_size,
            st_mtime_ns=result.st_mtime_ns + (1 if calls == 2 else 0),
            st_ctime_ns=result.st_ctime_ns)

    monkeypatch.setattr(admin.os, "fstat", altered_time)
    with pytest.raises(backup.ArtifactBackupError, match="changed"):
        admin.database_digest(path)


def test_database_metadata_reads_are_bounded(cli, monkeypatch):
    _, _, _, url = cli

    async def read():
        engine = create_async_engine(url)
        try:
            return await admin.read_records(async_sessionmaker(engine))
        finally:
            await engine.dispose()

    assert len(asyncio.run(read())) == 1
    monkeypatch.setattr(backup, "MAX_ENTRIES", 0)
    with pytest.raises(backup.ArtifactBackupError, match="limit"):
        asyncio.run(read())


@pytest.mark.parametrize("change", ["metadata", "dump"])
async def test_success_requires_unchanged_recovery_point(source, tmp_path, monkeypatch, change):
    from app import config, db

    root, row, _ = source
    dump = tmp_path / "dump"
    dump.write_bytes(b"synthetic recovery point")
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(artifact_root=str(root)))
    monkeypatch.setattr(db, "get_schema_readiness",
                        AsyncMock(return_value=SimpleNamespace(ready=True)))
    dispose = AsyncMock()
    monkeypatch.setattr(db, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(admin, "read_records", AsyncMock(side_effect=[
        [row], [replace(row, name="changed")] if change == "metadata" else [row],
    ]))
    original = admin.operate

    def changed(*args):
        result = original(*args)
        if change == "dump":
            dump.write_bytes(b"new recovery point")
        return result

    monkeypatch.setattr(admin, "operate", changed)
    args = admin.parser().parse_args(["create", "--database-dump", str(dump), "--output",
                                     str(tmp_path / "snapshot"), "--writers-stopped"])
    with pytest.raises(backup.ArtifactBackupError, match="recovery point changed"):
        await admin.execute(args)
    dispose.assert_awaited_once()
