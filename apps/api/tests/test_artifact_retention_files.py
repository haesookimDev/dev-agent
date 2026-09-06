import hashlib
import os
import uuid

import pytest

from app import artifact_retention_files as files
from app.artifact_storage import MAX_ARTIFACT_BYTES, write_artifact_content


@pytest.fixture
def content(tmp_path):
    work = str(uuid.uuid4())
    key = f"{work}/artifacts/nested/retained.txt"
    value = b"Synthetic retained bytes\n"
    path = write_artifact_content(str(tmp_path), work, key, value)
    return tmp_path, work, key, value, path, hashlib.sha256(value).hexdigest()


def test_exact_file_removal_preserves_siblings_parents_and_idempotent_retry(content):
    root, work, key, value, path, digest = content
    sibling = path.with_name("keep.txt")
    sibling.write_bytes(b"Never selected")
    assert files.inspect_file(root, work, key, len(value)) == digest
    assert path.read_bytes() == value
    assert files.purge_file(root, work, key, len(value), digest)
    assert not path.exists() and path.parent.is_dir()
    assert sibling.read_bytes() == b"Never selected"
    assert not files.purge_file(root, work, key, len(value), digest)
    assert files.inspect_file(root, work, key, len(value), expected=digest, missing_ok=True) is None
    with pytest.raises(files.RetentionFileError, match="^missing_content$"):
        files.inspect_file(root, work, key, len(value))


@pytest.mark.parametrize("change", [
    {"work": "invalid"}, {"work": None}, {"key": "../file"}, {"key": "/absolute"},
    {"key": "foreign/artifacts/file"}, {"key": "delivery.patch"}, {"key": "x" * 1025},
    {"size": True}, {"size": -1}, {"size": MAX_ARTIFACT_BYTES + 1},
    {"expected": None}, {"expected": "g" * 64}, {"expected": "A" * 64},
])
def test_invalid_inputs_never_open_or_unlink_any_path(content, monkeypatch, change):
    root, work, key, value, path, digest = content
    args = {"root": root, "work": work, "key": key, "size": len(value), "expected": digest}
    def forbidden(*_, **__):
        pytest.fail("invalid metadata must not open or delete files")
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", forbidden)
        patch.setattr(os, "unlink", forbidden)
        with pytest.raises(files.RetentionFileError):
            files.purge_file(**(args | change))
    assert path.read_bytes() == value


@pytest.mark.parametrize("kind", [
    "leaf-link", "parent-link", "missing-parent", "missing-root", "directory", "fifo",
    "wrong-size", "wrong-hash",
])
def test_unreachable_or_changed_storage_never_counts_as_success(content, kind):
    root, work, key, value, path, digest = content
    foreign = root / "foreign.txt"
    foreign.write_bytes(value)
    size = len(value)
    if kind in {"leaf-link", "directory", "fifo", "missing-parent", "parent-link"}:
        path.unlink()
        if kind == "leaf-link":
            path.symlink_to(foreign)
        elif kind == "directory":
            path.mkdir()
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            path.parent.rmdir()
            if kind == "parent-link":
                path.parent.symlink_to(root)
    elif kind == "missing-root":
        root = root / "not-mounted"
    elif kind == "wrong-size":
        size += 1
    else:
        digest = "0" * 64
    with pytest.raises(files.RetentionFileError):
        files.purge_file(root, work, key, size, digest)
    assert foreign.read_bytes() == value


def test_file_replaced_during_verification_is_not_deleted(content, monkeypatch):
    root, work, key, value, path, digest = content
    original_stat = os.fstat
    replaced = False
    def swapping(fd):
        nonlocal replaced
        state = original_stat(fd)
        if not replaced and state.st_size == len(value):
            replaced = True
            path.rename(path.with_name("original.txt"))
            path.write_bytes(b"New file must remain intact")
        return state
    monkeypatch.setattr(os, "fstat", swapping)
    with pytest.raises(files.RetentionFileError, match="^content_mismatch$"):
        files.purge_file(root, work, key, len(value), digest)
    assert path.read_bytes() == b"New file must remain intact"
    assert path.with_name("original.txt").read_bytes() == value


@pytest.mark.parametrize("operation", ["unlink", "fsync"])
def test_io_failure_is_private_and_fsync_retry_handles_already_unlinked_file(
    content, monkeypatch, operation,
):
    root, work, key, value, path, digest = content
    def failed(*_, **__):
        raise OSError("synthetic-private-path-and-mount-error")
    with monkeypatch.context() as patch:
        patch.setattr(os, operation, failed)
        with pytest.raises(files.RetentionFileError, match="^storage_unavailable$") as caught:
            files.purge_file(root, work, key, len(value), digest)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert path.exists() == (operation == "unlink")
    assert files.purge_file(root, work, key, len(value), digest) == (operation == "unlink")


def test_open_parent_swap_cannot_redirect_removal_to_foreign_work(content, monkeypatch):
    root, work, key, value, path, digest = content
    foreign = root / "foreign"
    other_file = foreign / "artifacts/nested/retained.txt"
    other_file.parent.mkdir(parents=True)
    other_file.write_bytes(value)
    original_open = os.open
    def swapping(component, flags, *args, **kwargs):
        descriptor = original_open(component, flags, *args, **kwargs)
        if component == work:
            (root / work).rename(root / "opened-work")
            (root / work).symlink_to(foreign, target_is_directory=True)
        return descriptor
    monkeypatch.setattr(os, "open", swapping)
    assert files.purge_file(root, work, key, len(value), digest)
    assert other_file.read_bytes() == value
    assert not (root / "opened-work/artifacts/nested/retained.txt").exists()
