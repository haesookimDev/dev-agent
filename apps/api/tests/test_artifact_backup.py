import json
import os
import stat
import uuid
from dataclasses import asdict, replace

import pytest

from app import artifact_backup as backup
from app.artifact_storage import MAX_ARTIFACT_BYTES

DATABASE_DIGEST = backup.digest(b"Synthetic database snapshot, not a credential")


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    work = str(uuid.uuid4())
    key = f"{work}/artifacts/검증 결과.txt"
    path = root / key
    path.parent.mkdir(parents=True)
    content = "Synthetic evidence 한글\n".encode()
    path.write_bytes(content)
    record = backup.ArtifactRecord(str(uuid.uuid4()), work, key, "evidence",
                                    "검증 결과 ✅.txt", "text/plain", len(content))
    return root, record, content


def create(source, tmp_path):
    root, record, _ = source
    snapshot = tmp_path / "snapshot"
    result = backup.create_snapshot(root, [record], snapshot, DATABASE_DIGEST)
    return snapshot, result["manifest_sha256"]


def test_round_trip_aliases_empty_files_permissions_and_reverification(source, tmp_path):
    root, record, content = source
    alias = replace(record, artifact_id=str(uuid.uuid4()), name="alias.txt")
    empty = replace(record, artifact_id=str(uuid.uuid4()), object_key=record.object_key + ".empty",
                    size_bytes=0, name="empty.txt")
    (root / empty.object_key).write_bytes(b"")
    rows = [empty, record, alias]
    snapshot, restored = tmp_path / "snapshot", tmp_path / "restored"
    result = backup.create_snapshot(root, rows, snapshot, DATABASE_DIGEST)
    sha = result["manifest_sha256"]
    assert result == {"artifacts": 3, "blobs": 2, "manifest_sha256": sha}
    assert backup.verify_snapshot(snapshot, list(reversed(rows)), DATABASE_DIGEST, sha)["verified"]
    assert backup.restore_snapshot(snapshot, rows, restored, DATABASE_DIGEST, sha) == {
        "artifacts": 3, "files": 2, "restored": True,
    }
    assert (restored / record.object_key).read_bytes() == content
    assert (restored / empty.object_key).read_bytes() == b""
    assert (root / record.object_key).read_bytes() == content
    assert backup.verify_restored(restored, rows, DATABASE_DIGEST, sha)["verified"]
    for directory in (snapshot, restored):
        for path in [directory, *directory.rglob("*")]:
            assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    (restored / record.object_key).write_bytes(b"x" * len(content))
    with pytest.raises(backup.ArtifactBackupError, match="restored artifact verification"):
        backup.verify_restored(restored, rows, DATABASE_DIGEST, sha)


def test_empty_snapshot_round_trip(tmp_path):
    snapshot, restored = tmp_path / "snapshot", tmp_path / "restored"
    result = backup.create_snapshot(tmp_path / "unused", [], snapshot, DATABASE_DIGEST)
    assert result["artifacts"] == result["blobs"] == 0
    assert backup.restore_snapshot(snapshot, [], restored, DATABASE_DIGEST,
                                   result["manifest_sha256"])["restored"]
    assert list(restored.iterdir()) == [restored / backup.COMPLETE]


@pytest.mark.parametrize("change", [
    {"artifact_id": "not-a-uuid"}, {"work_item_id": "../other"},
    {"object_key": "../outside"}, {"object_key": "other/artifacts/file"},
    {"size_bytes": True}, {"size_bytes": -1}, {"size_bytes": MAX_ARTIFACT_BYTES + 1},
    {"name": "x" * 256}, {"content_type": ["text/plain"]},
])
def test_invalid_metadata_is_rejected_before_destination_creation(source, tmp_path, change):
    root, record, _ = source
    destination = tmp_path / "new"
    with pytest.raises(backup.ArtifactBackupError):
        backup.create_snapshot(root, [replace(record, **change)], destination, DATABASE_DIGEST)
    assert not destination.exists()


def test_duplicate_identifiers_and_limits_are_rejected(source, tmp_path, monkeypatch):
    root, record, _ = source
    with pytest.raises(backup.ArtifactBackupError, match="duplicate"):
        backup.create_snapshot(root, [record, record], tmp_path / "duplicate", DATABASE_DIGEST)
    monkeypatch.setattr(backup, "MAX_ENTRIES", 0)
    with pytest.raises(backup.ArtifactBackupError, match="limit"):
        backup.create_snapshot(root, [record], tmp_path / "over-limit", DATABASE_DIGEST)


@pytest.mark.parametrize("kind", [
    "missing", "symlink", "parent-link", "fifo", "directory", "wrong-size",
])
def test_bad_source_bytes_never_publish_a_manifest(source, tmp_path, kind):
    root, record, content = source
    path = root / record.object_key
    path.unlink()
    if kind == "symlink":
        outside = tmp_path / "private"
        outside.write_bytes(content)
        path.symlink_to(outside)
    elif kind == "parent-link":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / path.name).write_bytes(content)
        path.parent.rmdir()
        path.parent.symlink_to(outside, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "wrong-size":
        path.write_bytes(content + b"changed")
    destination = tmp_path / "incomplete"
    with pytest.raises(backup.ArtifactBackupError):
        backup.create_snapshot(root, [record], destination, DATABASE_DIGEST)
    assert destination.is_dir()
    assert not (destination / backup.MANIFEST).exists()


@pytest.mark.parametrize("kind", ["manifest-hash", "database-hash", "database-rows", "blob",
                                         "missing-blob", "blob-link", "manifest-link"])
def test_snapshot_integrity_is_checked_before_restore_creates_anything(source, tmp_path, kind):
    _, record, content = source
    snapshot, sha = create(source, tmp_path)
    database = DATABASE_DIGEST
    rows = [record]
    blob = snapshot / "blobs" / backup.digest(content)
    if kind == "manifest-hash":
        sha = "0" * 64
    elif kind == "database-hash":
        database = "1" * 64
    elif kind == "database-rows":
        rows = [replace(record, name="other.txt")]
    elif kind == "blob":
        blob.write_bytes(b"x" * len(content))
    elif kind == "missing-blob":
        blob.unlink()
    else:
        path = snapshot / backup.MANIFEST if kind == "manifest-link" else blob
        outside = tmp_path / "outside"
        path.rename(outside)
        path.symlink_to(outside)
    destination = tmp_path / "restored"
    with pytest.raises(backup.ArtifactBackupError):
        backup.restore_snapshot(snapshot, rows, destination, database, sha)
    assert not destination.exists()


@pytest.mark.parametrize("mutate", [
    lambda m: {**m, "version": 3}, lambda m: {**m, "version": True},
    lambda m: {**m, "extra": "ignored?"}, lambda m: {**m, "artifacts": {}},
    lambda m: {**m, "artifacts": m["artifacts"] * 2},
    lambda m: {**m, "artifacts": [{**m["artifacts"][0], "sha256": "../outside"}]},
    lambda m: {**m, "artifacts": [{**m["artifacts"][0], "unknown": True}]},
])
def test_even_a_trusted_manifest_must_have_valid_schema(source, tmp_path, mutate):
    _, record, _ = source
    snapshot, _ = create(source, tmp_path)
    path = snapshot / backup.MANIFEST
    raw = backup.encode(mutate(json.loads(path.read_bytes())))
    path.write_bytes(raw)
    with pytest.raises(backup.ArtifactBackupError):
        backup.verify_snapshot(snapshot, [record], DATABASE_DIGEST, backup.digest(raw))


@pytest.mark.parametrize("raw", [b'{"version":1,"version":1}', b"{", b"[]", b"[" * 2000])
def test_malformed_or_duplicate_manifest_fields_fail_safely(source, tmp_path, raw):
    _, record, _ = source
    snapshot, _ = create(source, tmp_path)
    (snapshot / backup.MANIFEST).write_bytes(raw)
    with pytest.raises(backup.ArtifactBackupError):
        backup.verify_snapshot(snapshot, [record], DATABASE_DIGEST, backup.digest(raw))


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_existing_destination_is_never_adopted_or_modified(source, tmp_path, kind):
    root, record, _ = source
    snapshot, sha = create(source, tmp_path)
    target = tmp_path / "existing"
    if kind == "file":
        target.write_bytes(b"preserve")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(root, target_is_directory=True)
    before = target.lstat()
    for operation in (
        lambda: backup.create_snapshot(root, [record], target, DATABASE_DIGEST),
        lambda: backup.restore_snapshot(snapshot, [record], target, DATABASE_DIGEST, sha),
    ):
        with pytest.raises(backup.ArtifactBackupError):
            operation()
        assert target.lstat() == before
    if kind == "file":
        assert target.read_bytes() == b"preserve"
    elif kind == "directory":
        assert not list(target.iterdir())


def test_nested_destination_and_late_blob_change_do_not_publish_completion(source, tmp_path,
                                                                         monkeypatch):
    root, record, content = source
    with pytest.raises(backup.ArtifactBackupError, match="separate"):
        backup.create_snapshot(root, [record], root / "nested", DATABASE_DIGEST)
    snapshot, sha = create(source, tmp_path)
    with pytest.raises(backup.ArtifactBackupError, match="separate"):
        backup.restore_snapshot(snapshot, [record], snapshot / "nested", DATABASE_DIGEST, sha)
    original = backup.verify_snapshot

    def mutate_after_preflight(*args):
        result = original(*args)
        (snapshot / "blobs" / backup.digest(content)).write_bytes(b"x" * len(content))
        return result

    monkeypatch.setattr(backup, "verify_snapshot", mutate_after_preflight)
    restored = tmp_path / "incomplete"
    with pytest.raises(backup.ArtifactBackupError, match="integrity"):
        backup.restore_snapshot(snapshot, [record], restored, DATABASE_DIGEST, sha)
    assert restored.exists()
    assert not (restored / backup.COMPLETE).exists()
    assert (root / record.object_key).read_bytes() == content


def test_manifest_size_and_interrupted_publication_are_not_success(source, tmp_path, monkeypatch):
    root, record, _ = source
    original = backup.write_new

    def interrupt(root, relative, content):
        if relative.name.startswith(".pending-"):
            original(root, relative, content[:5])
            raise OSError("private disk diagnostic")
        original(root, relative, content)

    monkeypatch.setattr(backup, "write_new", interrupt)
    incomplete = tmp_path / "incomplete"
    with pytest.raises(backup.ArtifactBackupError) as error:
        backup.create_snapshot(root, [record], incomplete, DATABASE_DIGEST)
    assert "private disk diagnostic" not in str(error.value)
    assert not (incomplete / backup.MANIFEST).exists()
    monkeypatch.setattr(backup, "write_new", original)
    snapshot, sha = create(source, tmp_path)
    monkeypatch.setattr(backup, "MAX_MANIFEST_BYTES", 4)
    with pytest.raises(backup.ArtifactBackupError, match="large"):
        backup.verify_snapshot(snapshot, [record], DATABASE_DIGEST, sha)


def test_snapshot_metadata_is_exact_and_contains_no_database_rows(source, tmp_path):
    _, record, _ = source
    snapshot, _ = create(source, tmp_path)
    manifest = json.loads((snapshot / backup.MANIFEST).read_bytes())
    assert set(manifest) == {"version", "database_sha256", "artifacts"}
    entry = manifest["artifacts"][0]
    assert {key: value for key, value in entry.items() if key != "sha256"} == asdict(record)


@pytest.mark.parametrize("target", ["manifest", "blob"])
@pytest.mark.parametrize("kind", ["fifo", "directory", "oversized"])
def test_snapshot_input_is_bounded_regular_data(source, tmp_path, target, kind):
    _, record, content = source
    snapshot, sha = create(source, tmp_path)
    path = snapshot / (backup.MANIFEST if target == "manifest"
                       else "blobs/" + backup.digest(content))
    path.unlink()
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        limit = backup.MAX_MANIFEST_BYTES if target == "manifest" else MAX_ARTIFACT_BYTES
        with path.open("wb") as file:
            file.truncate(limit + 1)
    with pytest.raises(backup.ArtifactBackupError):
        backup.restore_snapshot(snapshot, [record], tmp_path / "restored", DATABASE_DIGEST, sha)
    assert not (tmp_path / "restored").exists()


def test_linked_blob_directory_is_rejected(source, tmp_path):
    _, record, _ = source
    snapshot, sha = create(source, tmp_path)
    (snapshot / "blobs").rename(tmp_path / "outside")
    (snapshot / "blobs").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(backup.ArtifactBackupError):
        backup.verify_snapshot(snapshot, [record], DATABASE_DIGEST, sha)


def test_equal_bytes_are_deduplicated_without_cross_work_metadata_aliasing(source, tmp_path):
    root, record, content = source
    other_work = str(uuid.uuid4())
    other = replace(record, artifact_id=str(uuid.uuid4()), work_item_id=other_work,
                    object_key=f"{other_work}/artifacts/other.txt", name="other.txt")
    (root / other.object_key).parent.mkdir(parents=True)
    (root / other.object_key).write_bytes(content)
    snapshot, restored = tmp_path / "snapshot", tmp_path / "restored"
    rows = [record, other]
    result = backup.create_snapshot(root, rows, snapshot, DATABASE_DIGEST)
    assert result["blobs"] == 1
    assert result["artifacts"] == 2
    assert backup.restore_snapshot(snapshot, rows, restored, DATABASE_DIGEST,
                                   result["manifest_sha256"])["files"] == 2
    assert (restored / other.object_key).read_bytes() == content
    assert (restored / record.object_key).read_bytes() == content
