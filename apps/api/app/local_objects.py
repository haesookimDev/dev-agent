"""Descriptor-anchored local access; callers validate their own object namespace."""

import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO


@contextmanager
def local_directory(
    root: Path, components: tuple[str, ...], *, create: bool = False,
) -> Iterator[int]:
    with ExitStack() as descriptors:
        # ARTIFACT_ROOT is operator-controlled; its alias may be intentional (e.g. /tmp).
        # Descendants are never resolved and reopened through a mutable original path.
        if create:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.callback(os.close, directory)
        for component in components:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass  # The following no-follow directory open validates the existing entry.
            directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            descriptors.callback(os.close, directory)
        yield directory


@contextmanager
def local_file(root: Path, relative: Path) -> Iterator[BinaryIO]:
    with local_directory(root, relative.parts[:-1]) as directory:
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            yield source
