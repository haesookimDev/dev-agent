"""Bounded basenames and ASCII-only HTTP serialization for untrusted artifact names."""

import unicodedata
from urllib.parse import quote


def valid_artifact_name(name: str) -> bool:
    return (0 < len(name) <= 255 and name == name.strip() and name not in {".", ".."}
            and not any(character in '"/\\' or unicodedata.category(character) in {"Cc", "Cs"}
                        for character in name))


def artifact_disposition(name: str) -> str:
    # Historical metadata is not rewritten; an invalid suggested name cannot break serving.
    if not valid_artifact_name(name):
        name = "artifact"
    # Some legacy clients decode percent escapes in filename; keep them only in filename*.
    fallback = name if name.isascii() and "%" not in name else "artifact"
    encoded = quote(name, safe="", encoding="utf-8", errors="strict")
    return f'inline; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'
