import hashlib
import os
import stat

import pytest
from delivery_fixtures import PATCH_CONTENT, PATCH_SHA256

from app.bundle_storage import (
    MAX_BUNDLE_BYTES,
    BundleIntegrityError,
    verified_bundle_bytes,
    write_bundle_snapshot,
)

PATCH_SIZE = len(PATCH_CONTENT)


@pytest.fixture
def stored(tmp_path):
    path = tmp_path / "work" / "delivery.patch"
    path.parent.mkdir()
    path.write_bytes(PATCH_CONTENT)
    return path


def read(root, path, digest=PATCH_SHA256, size=PATCH_SIZE):
    return verified_bundle_bytes(str(root), str(path), digest, size)


def test_verified_read_and_private_snapshot_do_not_follow_source_replacement(tmp_path, stored):
    content = read(tmp_path, stored)
    assert content == PATCH_CONTENT
    stored.write_bytes(b"Unapproved replacement")
    snapshot = write_bundle_snapshot(tmp_path, content)
    assert snapshot.read_bytes() == PATCH_CONTENT
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_bundle_snapshot(tmp_path, b"overwrite")
    assert snapshot.read_bytes() == PATCH_CONTENT


@pytest.mark.parametrize("change,code", [
    ("missing", "bundle_unavailable"), ("directory", "bundle_unavailable"),
    ("fifo", "bundle_unavailable"), ("leaf_link", "bundle_unavailable"),
    ("parent_link", "bundle_unavailable"), ("outside", "bundle_unavailable"),
    ("traversal", "bundle_unavailable"), ("empty", "bundle_integrity_failed"),
    ("truncated", "bundle_integrity_failed"), ("extended", "bundle_integrity_failed"),
    ("same_size", "bundle_integrity_failed"),
])
def test_unsafe_or_corrupt_objects_fail_without_disclosing_details(tmp_path, stored, change, code):
    path = stored
    if change in {"missing", "directory", "fifo", "leaf_link"}:
        stored.unlink()
        if change == "directory":
            stored.mkdir()
        elif change == "fifo":
            os.mkfifo(stored)
        elif change == "leaf_link":
            target = tmp_path / "private-target.patch"
            target.write_bytes(PATCH_CONTENT)
            stored.symlink_to(target)
    elif change == "parent_link":
        stored.parent.rename(tmp_path / "moved")
        stored.parent.symlink_to(tmp_path / "moved", target_is_directory=True)
    elif change == "outside":
        path = tmp_path.parent / "not-in-root.patch"
    elif change == "traversal":
        path = tmp_path / "work" / ".." / "work" / "delivery.patch"
    else:
        stored.write_bytes({"empty": b"", "truncated": PATCH_CONTENT[:-1],
                            "extended": PATCH_CONTENT + b"!",
                            "same_size": b"x" * len(PATCH_CONTENT)}[change])
    with pytest.raises(BundleIntegrityError) as error:
        read(tmp_path, path)
    assert error.value.code == str(error.value) == code
    assert error.value.__cause__ is None


@pytest.mark.parametrize("digest,size", [
    ("a", 1), (PATCH_SHA256.upper(), 1), (None, 1), (PATCH_SHA256, 0),
    (PATCH_SHA256, -1), (PATCH_SHA256, True), (PATCH_SHA256, MAX_BUNDLE_BYTES + 1),
])
def test_invalid_metadata_is_rejected_before_open(tmp_path, monkeypatch, digest, size):
    def unexpected(*_, **__):
        pytest.fail("invalid metadata must not open a path")
    monkeypatch.setattr(os, "open", unexpected)
    with pytest.raises(BundleIntegrityError, match="^bundle_integrity_failed$"):
        read(tmp_path, tmp_path / "private.patch", digest, size)


def test_maximum_size_and_operator_controlled_root_alias(tmp_path):
    root = tmp_path / "objects"
    root.mkdir()
    alias = tmp_path / "configured-root"
    alias.symlink_to(root, target_is_directory=True)
    content = b"x" * MAX_BUNDLE_BYTES
    path = alias / "delivery.patch"
    path.write_bytes(content)
    assert read(alias, path, hashlib.sha256(content).hexdigest(), len(content)) == content


def test_parent_replacement_does_not_redirect_an_open_directory(tmp_path, stored, monkeypatch):
    original = os.open

    def replaced(path, flags, *args, **kwargs):
        descriptor = original(path, flags, *args, **kwargs)
        if path == "work":
            stored.parent.rename(tmp_path / "original")
            replacement = tmp_path / "replacement"
            replacement.mkdir()
            (replacement / "delivery.patch").write_bytes(b"x" * len(PATCH_CONTENT))
            stored.parent.symlink_to(replacement, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(os, "open", replaced)
    assert read(tmp_path, stored) == PATCH_CONTENT


def test_open_failure_hides_private_filesystem_error(tmp_path, stored, monkeypatch):
    def denied(*_, **__):
        raise PermissionError("private-path-and-mount-details")
    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(BundleIntegrityError, match="^bundle_unavailable$") as error:
        read(tmp_path, stored)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__


def test_modification_during_read_is_rejected(tmp_path, stored, monkeypatch):
    original = os.fstat
    calls = 0

    def changed(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            stored.write_bytes(b"x" * PATCH_SIZE)
        return original(descriptor)

    monkeypatch.setattr(os, "fstat", changed)
    with pytest.raises(BundleIntegrityError, match="^bundle_integrity_failed$"):
        read(tmp_path, stored)
