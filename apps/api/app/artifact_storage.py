"""Work-scoped local artifact access; never trust a retained key as authorization."""

import os
import stat
import uuid
from contextlib import suppress
from pathlib import Path

from .local_objects import local_directory, local_file

MAX_ARTIFACT_BYTES = 10 * 1024 * 1024


class ArtifactStorageError(RuntimeError):
    """Only a fixed public message may escape the storage boundary."""


def artifact_path(work_item_id: str, object_key: str) -> Path:
    components = object_key.split("/")
    if (len(components) < 3 or components[:2] != [work_item_id, "artifacts"]
            or any(part in {"", ".", ".."} or "\\" in part or "\x00" in part
                   for part in components)):
        raise ValueError("artifact key must belong to this work")
    return Path(*components)


def write_artifact_content(root: str, work_item_id: str, object_key: str, content: bytes) -> Path:
    try:
        relative = artifact_path(work_item_id, object_key)
        if not 0 < len(content) <= MAX_ARTIFACT_BYTES:
            raise ValueError
        with local_directory(Path(root), relative.parts[:-1], create=True) as directory:
            temporary = f".{relative.name}.{uuid.uuid4().hex}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                                 dir_fd=directory)
            try:
                with os.fdopen(descriptor, "wb") as destination:
                    destination.write(content)
                os.replace(temporary, relative.name, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
        return Path(root) / relative
    except (OSError, ValueError):
        raise ArtifactStorageError("artifact storage is unavailable") from None


def read_artifact_content(root: str, work_item_id: str, object_key: str) -> bytes | None:
    try:
        relative = artifact_path(work_item_id, object_key)
        with local_file(Path(root), relative) as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= MAX_ARTIFACT_BYTES:
                return None
            content = source.read(before.st_size + 1)
            after = os.fstat(source.fileno())
            if (len(content) != before.st_size
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                return None
            return content
    except (OSError, ValueError):
        return None
