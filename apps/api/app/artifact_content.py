"""Artifact formats are data, never executable documents at the control-plane origin."""

import json

ALLOWED_ARTIFACT_TYPES = frozenset({
    "image/png", "image/jpeg", "image/webp", "text/plain", "application/json",
})


def artifact_content_matches(content_type: str, content: bytes) -> bool:
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    try:
        if content_type == "application/json":
            json.loads(content)
            return True
        if content_type == "text/plain":
            content.decode("utf-8")
            return True
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return False
    return False
