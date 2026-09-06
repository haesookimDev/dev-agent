import asyncio
import json
import os
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_artifact_backup import DATABASE_DIGEST, create
from test_artifact_backup import source as source
from test_artifact_backup_admin import cli as cli

from app import artifact_backup as backup
from app.artifact_backup_admin import read_records
from app.models import Artifact


def purged(row):
    expired_at = datetime(2026, 8, 1, tzinfo=UTC)
    return replace(row, expired_at=expired_at.isoformat(),
        purged_at=(expired_at + timedelta(seconds=1)).isoformat(), retention_days=30,
        retention_sha256=backup.digest(b"Expired synthetic bytes"))


def test_v2_retains_expiration_evidence_without_restoring_expired_bytes(source, tmp_path):
    root, live, content = source
    expired = purged(replace(live, artifact_id=str(uuid.uuid4()),
                             object_key=live.object_key + ".expired"))
    alias = replace(expired, artifact_id=str(uuid.uuid4()), name="expired alias.txt")
    rows = [live, expired, alias]
    snapshot, restored = tmp_path / "snapshot", tmp_path / "restored"
    result = backup.create_snapshot(root, rows, snapshot, DATABASE_DIGEST)
    raw = json.loads((snapshot / backup.MANIFEST).read_bytes())
    assert raw["version"] == 2 and result["blobs"] == 1
    entry = next(entry for entry in raw["artifacts"] if entry["artifact_id"] == expired.artifact_id)
    assert entry == {**asdict(expired), "sha256": None}
    sha = result["manifest_sha256"]
    assert backup.verify_snapshot(snapshot, rows, DATABASE_DIGEST, sha)["verified"]
    assert backup.restore_snapshot(snapshot, rows, restored, DATABASE_DIGEST, sha)["files"] == 1
    assert (restored / live.object_key).read_bytes() == content
    assert not (restored / expired.object_key).exists()
    assert backup.verify_restored(restored, rows, DATABASE_DIGEST, sha)["verified"]
    (restored / expired.object_key).write_bytes(b"Resurrected file")
    with pytest.raises(backup.ArtifactBackupError, match="remain absent"):
        backup.verify_restored(restored, rows, DATABASE_DIGEST, sha)


def test_all_expired_snapshot_restores_only_metadata_and_never_requires_blobs(source, tmp_path):
    root, row, _ = source
    row = purged(row)
    (root / row.object_key).unlink()
    snapshot, restored = tmp_path / "snapshot", tmp_path / "restored"
    result = backup.create_snapshot(root, [row], snapshot, DATABASE_DIGEST)
    assert result["blobs"] == 0 and not (snapshot / "blobs").exists()
    sha = result["manifest_sha256"]
    assert backup.restore_snapshot(snapshot, [row], restored, DATABASE_DIGEST, sha)["files"] == 0
    assert {path for path in restored.rglob("*") if path.is_file()} == {
        restored / backup.COMPLETE}
    assert (restored / row.object_key).parent.is_dir()
    assert backup.verify_restored(restored, [row], DATABASE_DIGEST, sha)["verified"]
    assert backup.create_snapshot(restored, [row], tmp_path / "next-snapshot",
                                   DATABASE_DIGEST)["blobs"] == 0


@pytest.mark.parametrize("change", [
    {"purged_at": None}, {"expired_at": None}, {"retention_days": None},
    {"retention_days": True}, {"retention_days": 0}, {"retention_days": 36501},
    {"retention_sha256": None}, {"retention_sha256": "g" * 64},
    {"expired_at": "2026-08-01T00:00:00"}, {"expired_at": "invalid"},
    {"purged_at": "2026-07-01T00:00:00+00:00"},
    {"expired_at": "2026-08-01T09:00:00+09:00"},
])
def test_pending_or_inconsistent_expiration_never_creates_destination(source, tmp_path, change):
    root, row, _ = source
    with pytest.raises(backup.ArtifactBackupError):
        backup.create_snapshot(root, [replace(purged(row), **change)], tmp_path / "new",
                               DATABASE_DIGEST)
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("kind", ["live", "different-policy", "different-size", "different-time"])
def test_aliases_cannot_disagree_about_expiration(source, tmp_path, kind):
    root, row, _ = source
    expired = purged(row)
    alias = replace(expired, artifact_id=str(uuid.uuid4()))
    if kind == "live":
        alias = replace(row, artifact_id=alias.artifact_id)
    elif kind == "different-policy":
        alias = replace(alias, retention_days=31)
    elif kind == "different-size":
        alias = replace(alias, size_bytes=0)
    else:
        alias = replace(alias, purged_at="2026-08-01T00:00:02+00:00")
    with pytest.raises(backup.ArtifactBackupError, match="aliased"):
        backup.create_snapshot(root, [expired, alias], tmp_path / "new", DATABASE_DIGEST)
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("kind", ["file", "link", "directory", "fifo", "missing-parent"])
def test_purged_source_must_retain_parents_but_have_no_file_entry(source, tmp_path, kind):
    root, row, _ = source
    path = root / row.object_key
    path.unlink()
    if kind == "file":
        path.write_bytes(b"Unexpected bytes")
    elif kind == "link":
        path.symlink_to(tmp_path / "missing-outside")
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.parent.rmdir()
    with pytest.raises(backup.ArtifactBackupError):
        backup.create_snapshot(root, [purged(row)], tmp_path / "snapshot", DATABASE_DIGEST)
    assert not (tmp_path / "snapshot" / backup.MANIFEST).exists()


def legacy_snapshot(source, tmp_path):
    snapshot, _ = create(source, tmp_path)
    raw = json.loads((snapshot / backup.MANIFEST).read_bytes())
    raw["version"] = 1
    raw["artifacts"] = [{key: value for key, value in entry.items() if key in backup.V1_FIELDS}
                        for entry in raw["artifacts"]]
    encoded = backup.encode(raw)
    (snapshot / backup.MANIFEST).write_bytes(encoded)
    return snapshot, backup.digest(encoded)


def test_v1_compatibility_requires_live_matching_database_rows(source, tmp_path):
    _, row, content = source
    snapshot, sha = legacy_snapshot(source, tmp_path)
    restored = tmp_path / "restored"
    assert backup.verify_snapshot(snapshot, [row], DATABASE_DIGEST, sha)["verified"]
    assert backup.restore_snapshot(snapshot, [row], restored, DATABASE_DIGEST, sha)["restored"]
    assert (restored / row.object_key).read_bytes() == content
    with pytest.raises(backup.ArtifactBackupError, match="metadata"):
        backup.restore_snapshot(snapshot, [purged(row)], tmp_path / "blocked", DATABASE_DIGEST, sha)
    assert not (tmp_path / "blocked").exists()


@pytest.mark.parametrize("change", ["v1-smuggling", "v2-missing", "expired-blob", "live-null-blob"])
def test_version_specific_entry_schema_cannot_smuggle_expiration(source, tmp_path, change):
    root, row, _ = source
    if change == "expired-blob":
        row = purged(row)
        (root / row.object_key).unlink()
    snapshot = tmp_path / "snapshot"
    backup.create_snapshot(root, [row], snapshot, DATABASE_DIGEST)
    raw = json.loads((snapshot / backup.MANIFEST).read_bytes())
    if change == "v1-smuggling":
        raw["version"] = 1
    elif change == "v2-missing":
        del raw["artifacts"][0]["expired_at"]
    elif change == "expired-blob":
        raw["artifacts"][0]["sha256"] = row.retention_sha256
    else:
        raw["artifacts"][0]["sha256"] = None
    encoded = backup.encode(raw)
    (snapshot / backup.MANIFEST).write_bytes(encoded)
    with pytest.raises(backup.ArtifactBackupError):
        backup.verify_snapshot(snapshot, [row], DATABASE_DIGEST, backup.digest(encoded))


def test_cli_reads_expiration_evidence_and_refuses_pending_snapshot(cli, source, tmp_path):
    run, _, dump, url = cli
    root, row, _ = source
    expired = purged(row)

    async def state(finish):
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.execute(sa.update(Artifact).values(
                    expired_at=datetime.fromisoformat(expired.expired_at), retention_days=30,
                    retention_sha256=expired.retention_sha256,
                    purged_at=datetime.fromisoformat(expired.purged_at) if finish else None))
            if finish:
                assert await read_records(async_sessionmaker(engine)) == [expired]
        finally:
            await engine.dispose()

    asyncio.run(state(False))
    args = ("create", "--database-dump", str(dump), "--output", str(tmp_path / "snapshot"),
            "--writers-stopped")
    run(*args, expected=2)
    assert not (tmp_path / "snapshot").exists()
    (root / row.object_key).unlink()
    asyncio.run(state(True))
    # Refresh the synthetic SQLite dump to match the coordinated expiration recovery point.
    import sqlite3
    with sqlite3.connect(cli[1]) as original, sqlite3.connect(dump) as target:
        original.backup(target)
    result = json.loads(run(*args).stdout)
    assert result["artifacts"] == 1 and result["blobs"] == 0 and result["verified"]
