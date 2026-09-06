"""Offline ordinary-artifact snapshots. Never extract archive paths or overwrite a root."""

import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .artifact_storage import MAX_ARTIFACT_BYTES, artifact_path, read_artifact_content
from .local_objects import local_directory, local_file

MAX_ENTRIES = 10_000
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MANIFEST = Path("manifest.json")
COMPLETE = Path(".kelpie-artifact-restore.json")


class ArtifactBackupError(RuntimeError):
    """Contains no paths, file contents or database values."""


@contextmanager
def safe_errors():
    try:
        yield
    except (OSError, ValueError, TypeError, RecursionError):
        raise ArtifactBackupError(
            "artifact snapshot operation failed; destination is not ready"
        ) from None


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    work_item_id: str
    object_key: str
    kind: str
    name: str
    content_type: str
    size_bytes: int


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def valid_digest(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def records(rows: list[ArtifactRecord]) -> list[ArtifactRecord]:
    if len(rows) > MAX_ENTRIES:
        raise ArtifactBackupError("artifact snapshot entry limit exceeded")
    seen = set()
    for row in rows:
        for value in (row.artifact_id, row.work_item_id):
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ArtifactBackupError("artifact snapshot requires canonical identifiers")
        if row.artifact_id in seen:
            raise ArtifactBackupError("artifact snapshot contains duplicate identifiers")
        seen.add(row.artifact_id)
        if (not isinstance(row.object_key, str) or len(row.object_key) > 1024
                or len(row.object_key.split("/")) > 64):
            raise ArtifactBackupError("invalid artifact snapshot key")
        artifact_path(row.work_item_id, row.object_key)
        if type(row.size_bytes) is not int or not 0 <= row.size_bytes <= MAX_ARTIFACT_BYTES:
            raise ArtifactBackupError("invalid artifact snapshot size")
        for value, maximum in ((row.kind, 64), (row.name, 255), (row.content_type, 128)):
            if not isinstance(value, str) or len(value) > maximum:
                raise ArtifactBackupError("invalid artifact snapshot metadata")
    return sorted(rows, key=lambda row: row.artifact_id)


def read_file(root: Path, relative: Path, maximum: int) -> bytes:
    with local_file(root, relative) as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= maximum:
            raise ArtifactBackupError("snapshot file is unavailable or too large")
        content = source.read(before.st_size + 1)
        after = os.fstat(source.fileno())
        if (len(content) != before.st_size or (before.st_size, before.st_mtime_ns,
                before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ArtifactBackupError("snapshot file changed during reading")
        return content


def write_new(root: Path, relative: Path, content: bytes) -> None:
    with local_directory(root, relative.parts[:-1], create=True) as directory:
        fd = os.open(relative.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                     dir_fd=directory)
        with os.fdopen(fd, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.fsync(directory)


def encode(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def new_destination(source: Path, destination: Path) -> None:
    if destination.resolve().is_relative_to(source.resolve()):
        raise ArtifactBackupError("snapshot source and destination must be separate")
    destination.mkdir(mode=0o700)  # Never adopt an existing directory or link.


def publish_manifest(root: Path, relative: Path, content: bytes) -> None:
    pending = Path(f".pending-{uuid.uuid4().hex}")
    write_new(root, pending, content)
    with local_directory(root, ()) as directory:
        # Hard-link publication is atomic and refuses an existing name, unlike rename/replace.
        os.link(pending.name, relative.name, src_dir_fd=directory, dst_dir_fd=directory,
                follow_symlinks=False)
        os.unlink(pending.name, dir_fd=directory)
        os.fsync(directory)


def create_snapshot(root: Path, rows: list[ArtifactRecord], destination: Path,
                    database_sha256: str) -> dict:
    """Caller coordinates a stopped-writer DB/file recovery point; no service is stopped here."""
    with safe_errors():
        expected = records(rows)
        if not valid_digest(database_sha256):
            raise ArtifactBackupError("invalid database snapshot digest")
        new_destination(root, destination)
        entries = []
        written = set()
        keys = {}
        for row in expected:
            content = read_artifact_content(str(root), row.work_item_id, row.object_key)
            if content is None or len(content) != row.size_bytes:
                raise ArtifactBackupError("artifact bytes do not match the database snapshot")
            sha256 = digest(content)
            if row.object_key in keys and keys[row.object_key] != sha256:
                raise ArtifactBackupError("aliased artifact changed during backup")
            keys[row.object_key] = sha256
            if sha256 not in written:
                write_new(destination, Path("blobs") / sha256, content)
                written.add(sha256)
            entries.append({**asdict(row), "sha256": sha256})
        manifest = encode({"version": 1, "database_sha256": database_sha256, "artifacts": entries})
        if len(manifest) > MAX_MANIFEST_BYTES:
            raise ArtifactBackupError("artifact snapshot manifest limit exceeded")
        # Only a complete snapshot has a manifest. Incomplete destinations remain quarantined.
        publish_manifest(destination, MANIFEST, manifest)
        return {"artifacts": len(entries), "blobs": len(written),
                "manifest_sha256": digest(manifest)}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactBackupError("duplicate snapshot manifest field")
        result[key] = value
    return result


def checked_manifest(
    backup: Path, rows: list[ArtifactRecord], database_sha256: str,
    manifest_sha256: str, manifest_path: Path = MANIFEST,
) -> tuple[bytes, list[dict]]:
    expected = records(rows)
    if not valid_digest(database_sha256) or not valid_digest(manifest_sha256):
        raise ArtifactBackupError("trusted snapshot digests are required")
    raw = read_file(backup, manifest_path, MAX_MANIFEST_BYTES)
    if digest(raw) != manifest_sha256:
        raise ArtifactBackupError("snapshot manifest digest mismatch")
    manifest = json.loads(raw, object_pairs_hook=unique_object)
    if (not isinstance(manifest, dict)
            or set(manifest) != {"version", "database_sha256", "artifacts"}
            or type(manifest["version"]) is not int or manifest["version"] != 1
            or manifest["database_sha256"] != database_sha256
            or not isinstance(manifest["artifacts"], list)
            or len(manifest["artifacts"]) > MAX_ENTRIES):
        raise ArtifactBackupError("snapshot format or database digest mismatch")
    entries = manifest["artifacts"]
    restored = []
    keys = {}
    for entry in entries:
        if not isinstance(entry, dict) or not valid_digest(entry.get("sha256")):
            raise ArtifactBackupError("invalid snapshot entry")
        restored.append(ArtifactRecord(**{
            key: value for key, value in entry.items() if key != "sha256"
        }))
        key = restored[-1].object_key
        if key in keys and keys[key] != entry["sha256"]:
            raise ArtifactBackupError("inconsistent aliased snapshot entries")
        keys[key] = entry["sha256"]
    if records(restored) != expected:
        raise ArtifactBackupError("snapshot metadata does not match the restored database")
    return raw, entries


def blob_content(backup: Path, entry: dict) -> bytes:
    content = read_file(backup, Path("blobs") / entry["sha256"], MAX_ARTIFACT_BYTES)
    if len(content) != entry["size_bytes"] or digest(content) != entry["sha256"]:
        raise ArtifactBackupError("snapshot blob integrity mismatch")
    return content


def verify_snapshot(backup: Path, rows: list[ArtifactRecord], database_sha256: str,
                    manifest_sha256: str) -> dict:
    with safe_errors():
        _, entries = checked_manifest(backup, rows, database_sha256, manifest_sha256)
        verified = set()
        for entry in entries:
            if (entry["sha256"], entry["size_bytes"]) not in verified:
                blob_content(backup, entry)
                verified.add((entry["sha256"], entry["size_bytes"]))
        return {"artifacts": len(entries), "blobs": len(verified), "verified": True}


def restore_snapshot(backup: Path, rows: list[ArtifactRecord], destination: Path,
                     database_sha256: str, manifest_sha256: str) -> dict:
    with safe_errors():
        verify_snapshot(backup, rows, database_sha256, manifest_sha256)
        raw, entries = checked_manifest(backup, rows, database_sha256, manifest_sha256)
        new_destination(backup, destination)
        written = set()
        for entry in entries:
            if entry["object_key"] in written:
                continue
            # Read and hash again while copying: validation must not authorize a later mutation.
            content = blob_content(backup, entry)
            relative = artifact_path(entry["work_item_id"], entry["object_key"])
            write_new(destination, relative, content)
            written.add(entry["object_key"])
        verify_contents(destination, entries)
        # Operators must not activate a root without this marker and the separate DB/access gates.
        publish_manifest(destination, COMPLETE, raw)
        return {"artifacts": len(entries), "files": len(written), "restored": True}


def verify_contents(root: Path, entries: list[dict]) -> None:
    for entry in entries:
        content = read_artifact_content(str(root), entry["work_item_id"], entry["object_key"])
        if (content is None or len(content) != entry["size_bytes"]
                or digest(content) != entry["sha256"]):
            raise ArtifactBackupError("restored artifact verification failed")


def verify_restored(root: Path, rows: list[ArtifactRecord], database_sha256: str,
                    manifest_sha256: str) -> dict:
    with safe_errors():
        _, entries = checked_manifest(root, rows, database_sha256, manifest_sha256, COMPLETE)
        verify_contents(root, entries)
        return {"artifacts": len(entries), "verified": True}
