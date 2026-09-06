"""Bounded, descriptor-anchored reads of the exact approved delivery bytes."""

import hashlib
import os
import re
import stat
from pathlib import Path

from .local_objects import local_file

MAX_BUNDLE_BYTES = 20 * 1024 * 1024


class BundleIntegrityError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        # Never expose the path, bytes, expected/actual digest or underlying OS exception.
        super().__init__(code)


def verified_bundle_bytes(root: str, object_path: str, digest: str, size_bytes: int) -> bytes:
    if (type(size_bytes) is not int or not 0 < size_bytes <= MAX_BUNDLE_BYTES
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
        raise BundleIntegrityError("bundle_integrity_failed")
    try:
        root_path = Path(root).absolute()
        path = Path(object_path).absolute()
        if ".." in path.parts or ".." in root_path.parts:
            raise ValueError
        relative = path.relative_to(root_path)
        if not relative.parts:
            raise ValueError
        with local_file(root_path, relative) as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise BundleIntegrityError("bundle_unavailable")
            if before.st_size != size_bytes:
                raise BundleIntegrityError("bundle_integrity_failed")
            content = source.read(size_bytes + 1)
            after = os.fstat(source.fileno())
            if (len(content) != size_bytes or hashlib.sha256(content).hexdigest() != digest
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise BundleIntegrityError("bundle_integrity_failed")
            return content
    except (OSError, ValueError):
        raise BundleIntegrityError("bundle_unavailable") from None


def write_bundle_snapshot(directory: Path, content: bytes) -> Path:
    """Write already-verified bytes outside the checkout, in an owned private workspace."""
    path = directory / "approved.patch"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(content)
    return path
