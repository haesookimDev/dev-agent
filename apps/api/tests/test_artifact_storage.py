import os
import stat

import pytest

from app.artifact_storage import (
    MAX_ARTIFACT_BYTES,
    ArtifactStorageError,
    artifact_path,
    read_artifact_content,
    write_artifact_content,
)

WORK = "own-work"
KEY = f"{WORK}/artifacts/evidence.txt"
CONTENT = b"Owned synthetic artifact\n"


@pytest.mark.parametrize("key", [
    "another-work/artifacts/evidence.txt", f"{WORK}-other/artifacts/evidence.txt",
    f"{WORK}/delivery.patch", f"{WORK}/artifacts", f"{WORK}/artifacts/",
    f"/{KEY}", f"./{KEY}", f"{WORK}//artifacts/file", f"{WORK}/artifacts/./file",
    f"{WORK}/artifacts/../file", f"{WORK}/artifacts/sub\\file", f"{KEY}\x00", "",
])
def test_invalid_namespace_never_opens_storage(tmp_path, monkeypatch, key):
    def forbidden(*_, **__):
        pytest.fail("invalid keys must not open any filesystem path")
    monkeypatch.setattr(os, "open", forbidden)
    with pytest.raises(ValueError, match="^artifact key must belong to this work$"):
        artifact_path(WORK, key)
    assert read_artifact_content(str(tmp_path), WORK, key) is None
    with pytest.raises(ArtifactStorageError, match="^artifact storage is unavailable$"):
        write_artifact_content(str(tmp_path), WORK, key, CONTENT)


def test_private_atomic_write_and_owned_nested_read(tmp_path):
    root = tmp_path / "configured-root"
    key = f"{WORK}/artifacts/nested/evidence.txt"
    path = write_artifact_content(str(root), WORK, key, CONTENT)
    assert path == root / key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert read_artifact_content(str(root), WORK, key) == CONTENT
    write_artifact_content(str(root), WORK, key, b"Complete replacement\n")
    assert read_artifact_content(str(root), WORK, key) == b"Complete replacement\n"
    assert not list(root.rglob("*.tmp"))


@pytest.mark.parametrize("entry", ["missing", "directory", "fifo", "leaf_link", "parent_link"])
def test_non_regular_and_linked_artifacts_are_unavailable(tmp_path, entry):
    path = tmp_path / KEY
    path.parent.mkdir(parents=True)
    if entry == "directory":
        path.mkdir()
    elif entry == "fifo":
        os.mkfifo(path)
    elif entry in {"leaf_link", "parent_link"}:
        private = tmp_path / "another-work" / "artifacts"
        private.mkdir(parents=True)
        target = private / path.name
        target.write_bytes(CONTENT)
        if entry == "leaf_link":
            path.symlink_to(target)
        else:
            path.parent.rmdir()
            path.parent.symlink_to(private, target_is_directory=True)
    assert read_artifact_content(str(tmp_path), WORK, KEY) is None


def test_reads_are_bounded_even_for_retained_metadata(tmp_path):
    path = write_artifact_content(str(tmp_path), WORK, KEY, b"x" * MAX_ARTIFACT_BYTES)
    assert len(read_artifact_content(str(tmp_path), WORK, KEY)) == MAX_ARTIFACT_BYTES
    with path.open("ab") as target:
        target.write(b"!")
    assert read_artifact_content(str(tmp_path), WORK, KEY) is None
    with pytest.raises(ArtifactStorageError):
        write_artifact_content(str(tmp_path), WORK, KEY, b"x" * (MAX_ARTIFACT_BYTES + 1))


def test_partial_write_failure_preserves_existing_content_and_removes_only_owned_temp(
    tmp_path, monkeypatch,
):
    path = write_artifact_content(str(tmp_path), WORK, KEY, CONTENT)
    foreign = path.with_name("unrelated.tmp")
    foreign.write_bytes(b"Keep this file")

    def failed(*_, **__):
        raise OSError("synthetic-private-mount-detail")
    monkeypatch.setattr(os, "replace", failed)
    with pytest.raises(ArtifactStorageError) as error:
        write_artifact_content(str(tmp_path), WORK, KEY, b"Replacement")
    assert str(error.value) == "artifact storage is unavailable"
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert path.read_bytes() == CONTENT
    assert set(path.parent.iterdir()) == {path, foreign}


def test_open_parent_replacement_cannot_redirect_write_to_foreign_work(tmp_path, monkeypatch):
    original = write_artifact_content(str(tmp_path), WORK, KEY, CONTENT)
    foreign = tmp_path / "foreign-work"
    (foreign / "artifacts").mkdir(parents=True)
    foreign_file = foreign / "artifacts" / original.name
    foreign_file.write_bytes(b"Unchanged foreign content")
    actual_open = os.open

    def replacing(component, flags, *args, **kwargs):
        descriptor = actual_open(component, flags, *args, **kwargs)
        if component == WORK:
            (tmp_path / WORK).rename(tmp_path / "original-work")
            (tmp_path / WORK).symlink_to(foreign, target_is_directory=True)
        return descriptor
    monkeypatch.setattr(os, "open", replacing)
    write_artifact_content(str(tmp_path), WORK, KEY, b"Owned new content")
    assert foreign_file.read_bytes() == b"Unchanged foreign content"
    assert (tmp_path / "original-work/artifacts/evidence.txt").read_bytes() == b"Owned new content"
    assert read_artifact_content(str(tmp_path), WORK, KEY) is None


def test_existing_destination_link_is_replaced_without_modifying_its_target(tmp_path):
    target = tmp_path / "foreign.txt"
    target.write_bytes(b"Unchanged foreign content")
    path = tmp_path / KEY
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    write_artifact_content(str(tmp_path), WORK, KEY, CONTENT)
    assert not path.is_symlink() and path.read_bytes() == CONTENT
    assert target.read_bytes() == b"Unchanged foreign content"
