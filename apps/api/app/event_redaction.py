"""Sanitize accepted worker telemetry without trusting a particular client.

The policy mirrors the independently deployed Runner; contract tests prevent
drift without adding a Runner runtime dependency to the control plane.
"""

from typing import Any

_CREDENTIAL_FIELDS = frozenset({
    "token", "accesstoken", "refreshtoken", "idtoken", "sessiontoken",
    "authorization", "proxyauthorization", "cookie", "setcookie", "apikey",
    "secret", "clientsecret", "password", "passwd", "privatekey",
    "leasetoken", "xkelpielease", "kelpieleasetoken",
})


def redact_worker_telemetry(value: Any, *, lease: str | None) -> Any:
    """Copy JSON telemetry; hide explicit credential fields and this plaintext lease.

    Call after lease validation, before persistence. This neither authenticates
    the caller nor detects arbitrary/encoded credentials or cleans old evidence.
    """
    if isinstance(value, dict):
        return {
            redact_worker_telemetry(key, lease=lease): (
                "[REDACTED]"
                if key.lower().replace("_", "").replace("-", "") in _CREDENTIAL_FIELDS
                else redact_worker_telemetry(item, lease=lease)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_worker_telemetry(item, lease=lease) for item in value]
    if isinstance(value, str) and lease:
        return value.replace(lease, "[REDACTED]")
    return value
