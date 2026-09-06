"""Bounded, descriptor-anchored reads of the exact approved delivery bytes."""

import hashlib
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path

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
        with ExitStack() as descriptors:
            # ARTIFACT_ROOT is operator-controlled (e.g. macOS /tmp may itself be a link).
            # Never resolve/check then reopen descendants: each open is anchored to its parent.
            directory = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY)
            descriptors.callback(os.close, directory)
            for component in relative.parts[:-1]:
                directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory)
                descriptors.callback(os.close, directory)
            descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
            with os.fdopen(descriptor, "rb") as source:
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
