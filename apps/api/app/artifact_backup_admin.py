"""Offline control-host CLI; reads database metadata, never starts or switches services."""

import argparse
import asyncio
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from . import artifact_backup as backup
from .local_objects import local_file
from .models import Artifact


def database_digest(path: Path) -> str:
    """Stream a stable regular dump file; hashing does not validate or restore its SQL."""
    with backup.safe_errors(), local_file(path.parent, Path(path.name)) as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size == 0:
            raise backup.ArtifactBackupError("database dump must be a nonempty regular file")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            content = source.read(min(remaining, 1024 * 1024))
            if not content:
                raise backup.ArtifactBackupError("database dump changed during reading")
            digest.update(content)
            remaining -= len(content)
        extra = source.read(1)
        after = os.fstat(source.fileno())
        if (extra or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise backup.ArtifactBackupError("database dump changed during reading")
        return digest.hexdigest()


async def read_records(sessions) -> list[backup.ArtifactRecord]:
    async with asyncio.timeout(10), sessions() as session:
        rows = (await session.scalars(select(Artifact).order_by(Artifact.id)
                                      .limit(backup.MAX_ENTRIES + 1))).all()
        return backup.records([
            backup.ArtifactRecord(row.id, row.work_item_id, row.object_key, row.kind,
                row.name, row.content_type, row.size_bytes, timestamp(row.expired_at),
                timestamp(row.purged_at), row.retention_days, row.retention_sha256)
            for row in rows
        ])


def timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    # Application SQLite timestamps are stored without an offset and represent UTC.
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()


def operate(arguments, root, rows, database_sha256):
    if arguments.command == "create":
        result = backup.create_snapshot(root, rows, arguments.output, database_sha256)
        verified = backup.verify_snapshot(arguments.output, rows, database_sha256,
                                           result["manifest_sha256"])
        return {**result, "verified": verified["verified"]}
    if arguments.command == "verify":
        return backup.verify_snapshot(arguments.backup, rows, database_sha256,
                                      arguments.manifest_sha256)
    if arguments.command == "restore":
        result = backup.restore_snapshot(arguments.backup, rows, arguments.output,
                                          database_sha256, arguments.manifest_sha256)
        verified = backup.verify_restored(arguments.output, rows, database_sha256,
                                           arguments.manifest_sha256)
        return {**result, "verified": verified["verified"]}
    return backup.verify_restored(root, rows, database_sha256, arguments.manifest_sha256)


async def execute(arguments) -> dict:
    from .config import get_settings
    from .db import SessionLocal, engine, get_schema_readiness

    try:
        if not (await get_schema_readiness()).ready:
            raise backup.ArtifactBackupError("database schema must match the deployed API")
        rows = await read_records(SessionLocal)
        database_sha256 = await asyncio.to_thread(database_digest, arguments.database_dump)
        result = await asyncio.to_thread(operate, arguments,
            Path(get_settings().artifact_root), rows, database_sha256)
        # Detect some coordination failures, not a substitute for stopping all writers.
        if (await read_records(SessionLocal) != rows
                or await asyncio.to_thread(database_digest, arguments.database_dump)
                != database_sha256):
            raise backup.ArtifactBackupError("database recovery point changed; do not activate")
        return result
    finally:
        await engine.dispose()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Back up or restore ordinary artifacts on an isolated control host. "
                    "Uses DATABASE_URL and ARTIFACT_ROOT; no database or service is changed.")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("create", "verify", "restore", "verify-restored"):
        child = commands.add_parser(name)
        child.add_argument("--database-dump", type=Path, required=True,
                           help="matching trusted DB dump; only its digest is read")
        if name != "create":
            child.add_argument("--manifest-sha256", required=True,
                               help="SHA-256 from separately protected trusted inventory")
        if name in ("create", "restore"):
            child.add_argument("--output", type=Path, required=True,
                               help="new directory in an existing restricted parent")
            child.add_argument("--writers-stopped", action="store_true", required=True,
                               help="acknowledge coordinated DB/file recovery point; "
                                    "this command does not stop writers")
        if name in ("verify", "restore"):
            child.add_argument("--backup", type=Path, required=True)
    return root


def main() -> None:
    command_parser = parser()
    arguments = command_parser.parse_args()
    try:
        result = asyncio.run(execute(arguments))
    except Exception:
        command_parser.exit(2, "artifact operation failed; do not activate the destination; "
                               "no private values were printed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
