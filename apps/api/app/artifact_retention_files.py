"""Exact-file retention IO. Caller must hold the control-plane writer fences."""

import hashlib
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from .artifact_storage import MAX_ARTIFACT_BYTES, artifact_path
from .local_objects import local_directory


class RetentionFileError(RuntimeError):
    """Fixed reason code only; never exposes paths or OS diagnostics."""


def signature(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


@contextmanager
def verified_file(root: Path, work: str, key: str, size: int, *, expected: str | None = None,
                  missing_ok: bool = False):
    try:
        if (not isinstance(work, str) or str(uuid.UUID(work)) != work
                or not isinstance(key, str) or len(key) > 1024
                or len(key.split("/")) > 64):
            raise RetentionFileError("invalid_path")
        relative = artifact_path(work, key)
        if type(size) is not int or not 0 <= size <= MAX_ARTIFACT_BYTES:
            raise RetentionFileError("invalid_size")
        if expected is not None and re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise RetentionFileError("invalid_digest")
        with local_directory(root, relative.parts[:-1]) as directory:
            try:
                descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=directory)
            except FileNotFoundError:
                if not missing_ok:
                    raise RetentionFileError("missing_content") from None
                yield directory, relative.name, None
                return
            with os.fdopen(descriptor, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                    raise RetentionFileError("content_mismatch")
                content = source.read(size + 1)
                sha256 = hashlib.sha256(content).hexdigest()
                if (len(content) != size or signature(before) != signature(os.fstat(descriptor))
                        or signature(before) != signature(os.stat(relative.name,
                            dir_fd=directory, follow_symlinks=False))
                        or (expected is not None and sha256 != expected)):
                    raise RetentionFileError("content_mismatch")
                yield directory, relative.name, sha256
    except (OSError, ValueError, TypeError):
        raise RetentionFileError("storage_unavailable") from None


def inspect_file(root: Path, work: str, key: str, size: int, *, expected: str | None = None,
                 missing_ok: bool = False) -> str | None:
    with verified_file(root, work, key, size, expected=expected, missing_ok=missing_ok) as value:
        return value[2]


def purge_file(root: Path, work: str, key: str, size: int, expected: str) -> bool:
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise RetentionFileError("invalid_digest")
    with verified_file(root, work, key, size, expected=expected, missing_ok=True) as value:
        directory, name, digest = value
        if digest is not None:
            os.unlink(name, dir_fd=directory)
        # Also synchronize a retry after unlink succeeded but its DB commit failed.
        os.fsync(directory)
        return digest is not None
