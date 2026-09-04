import hashlib
import hmac

import pytest
from fastapi import HTTPException

from app.integrations.slack import verify_signature


def signature(body: bytes, timestamp: int, secret: str) -> str:
    base = b"v0:" + str(timestamp).encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_valid_slack_signature() -> None:
    body = b"command=%2Fkelpie&text=feedback+id+fix+it"
    timestamp = 2_000_000_000
    verify_signature(
        body,
        str(timestamp),
        signature(body, timestamp, "signing-secret"),
        "signing-secret",
        now=timestamp,
    )


def test_stale_slack_signature_is_rejected() -> None:
    body = b"command=%2Fkelpie"
    with pytest.raises(HTTPException) as error:
        verify_signature(
            body,
            "1000",
            signature(body, 1000, "signing-secret"),
            "signing-secret",
            now=2000,
        )
    assert error.value.status_code == 401
