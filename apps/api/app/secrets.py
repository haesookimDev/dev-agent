"""Secret sources with request-time reads and no fallback from a configured file."""

import os
import stat
from dataclasses import dataclass, field
from typing import Protocol

MAX_SECRET_BYTES = 64 * 1024


class SecretUnavailableError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("configured secret is unavailable")


class SecretProvider(Protocol):
    def read(self) -> str: ...


@dataclass(frozen=True)
class EnvironmentSecretProvider:
    value: str = field(repr=False)

    def read(self) -> str:
        return self.value


@dataclass(frozen=True)
class FileSecretProvider:
    path: str = field(repr=False)

    def read(self) -> str:
        # Re-open on every use: projected-volume symlinks and atomic replacements
        # must take effect without retaining a descriptor or the previous value.
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise SecretUnavailableError()
                with os.fdopen(descriptor, "rb", closefd=False) as source:
                    contents = source.read(MAX_SECRET_BYTES + 1)
            finally:
                os.close(descriptor)
            if len(contents) > MAX_SECRET_BYTES:
                raise SecretUnavailableError()
            value = contents.decode("utf-8").rstrip("\r\n")
            if not value.strip() or "\x00" in value:
                raise SecretUnavailableError()
            return value
        except (OSError, UnicodeError, ValueError):
            # Paths and original exceptions can contain confidential material.
            raise SecretUnavailableError() from None
