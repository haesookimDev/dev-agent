import contextvars
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

CORRELATION_HEADER = "X-Kelpie-Correlation-ID"
REQUEST_HEADER = "X-Request-ID"

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kelpie_correlation_id", default=None
)
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kelpie_request_id", default=None
)


def new_id() -> str:
    return str(uuid.uuid4())


def valid_id(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    canonical = str(parsed)
    return canonical if value.lower() == canonical else None


def current_correlation_id() -> str:
    value = _correlation_id.get()
    if value is None:
        value = current_request_id()
        _correlation_id.set(value)
    return value


def current_request_id() -> str:
    value = _request_id.get()
    if value is None:
        value = new_id()
        _request_id.set(value)
    return value


class CorrelationMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        request_id = valid_id(_decoded(headers.get(REQUEST_HEADER.lower().encode()))) or new_id()
        correlation_id = (
            valid_id(_decoded(headers.get(CORRELATION_HEADER.lower().encode()))) or request_id
        )
        request_token = _request_id.set(request_id)
        correlation_token = _correlation_id.set(correlation_id)

        async def send_with_ids(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.extend(
                    [
                        (REQUEST_HEADER.lower().encode(), request_id.encode()),
                        (CORRELATION_HEADER.lower().encode(), correlation_id.encode()),
                    ]
                )
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_ids)
        finally:
            _correlation_id.reset(correlation_token)
            _request_id.reset(request_token)


def _decoded(value: bytes | None) -> str | None:
    return value.decode("ascii", errors="ignore") if value else None
