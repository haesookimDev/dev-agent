import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram
from prometheus_client.exposition import generate_latest

from .config import Settings
from .correlation import current_correlation_id, current_request_id

logger = logging.getLogger(__name__)

REGISTRY = CollectorRegistry()
HTTP_REQUESTS = Counter(
    "kelpie_http_requests_total",
    "HTTP requests handled by the control plane.",
    ("method", "route", "status"),
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "kelpie_http_request_duration_seconds",
    "Control-plane HTTP request duration.",
    ("method", "route"),
    registry=REGISTRY,
)
WORK_CLAIMS = Counter(
    "kelpie_work_claims_total",
    "Worker claim attempts by outcome.",
    ("outcome",),
    registry=REGISTRY,
)
QUEUE_WAIT = Histogram(
    "kelpie_work_queue_wait_seconds",
    "Time work spends queued before a worker claim.",
    registry=REGISTRY,
)
WORK_TRANSITIONS = Counter(
    "kelpie_work_transitions_total",
    "Work-item state transitions.",
    ("from_status", "to_status"),
    registry=REGISTRY,
)
STATE_DURATION = Histogram(
    "kelpie_work_state_duration_seconds",
    "Time work spends in a state before transition.",
    ("status",),
    registry=REGISTRY,
)
APPROVALS = Counter(
    "kelpie_approvals_total",
    "Approval decisions by kind and decision.",
    ("kind", "decision"),
    registry=REGISTRY,
)
DELIVERY_ATTEMPTS = Counter(
    "kelpie_delivery_attempts_total",
    "Delivery attempts started by the control plane.",
    ("attempt_type",),
    registry=REGISTRY,
)
DELIVERY_OUTCOMES = Counter(
    "kelpie_delivery_outcomes_total",
    "Delivery results by outcome.",
    ("outcome",),
    registry=REGISTRY,
)

tracer = trace.get_tracer("kelpie.control-plane")
_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": current_request_id(),
            "correlation_id": current_correlation_id(),
        }
        for name in ("work_id", "event_type", "outcome", "method", "route", "status"):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_observability(settings: Settings) -> None:
    global _configured, tracer
    if _configured:
        return

    if settings.log_format == "json":
        root = logging.getLogger()
        if not root.handlers:
            root.addHandler(logging.StreamHandler())
        formatter = JsonFormatter()
        for handler in root.handlers:
            handler.setFormatter(formatter)

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    if settings.otel_exporter_otlp_traces_endpoint:
        exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_traces_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("kelpie.control-plane")
    _configured = True


class ObservabilityMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        method = scope.get("method", "UNKNOWN")
        response_status = 500
        with tracer.start_as_current_span("http.request", kind=SpanKind.SERVER) as span:
            span.set_attribute("http.request.method", method)
            span.set_attribute("url.path", scope.get("path", ""))
            span.set_attribute("kelpie.request_id", current_request_id())
            span.set_attribute("kelpie.correlation_id", current_correlation_id())

            async def observe_response(message: dict[str, Any]) -> None:
                nonlocal response_status
                if message["type"] == "http.response.start":
                    response_status = message["status"]
                    span.set_attribute("http.response.status_code", response_status)
                    if response_status >= 500:
                        span.set_status(Status(StatusCode.ERROR))
                await send(message)

            try:
                await self.app(scope, receive, observe_response)
            except Exception as error:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
                raise
            finally:
                route = getattr(scope.get("route"), "path", "unmatched")
                duration = time.perf_counter() - started
                HTTP_REQUESTS.labels(method=method, route=route, status=str(response_status)).inc()
                HTTP_DURATION.labels(method=method, route=route).observe(duration)
                logger.info(
                    "http request completed",
                    extra={
                        "method": method,
                        "route": route,
                        "status": response_status,
                        "outcome": "error" if response_status >= 500 else "success",
                    },
                )


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def observe_claim(outcome: str, *, queued_seconds: float | None = None) -> None:
    WORK_CLAIMS.labels(outcome=outcome).inc()
    if queued_seconds is not None:
        QUEUE_WAIT.observe(max(0, queued_seconds))


def observe_transition(from_status: str, to_status: str, state_seconds: float) -> None:
    WORK_TRANSITIONS.labels(from_status=from_status, to_status=to_status).inc()
    STATE_DURATION.labels(status=from_status).observe(max(0, state_seconds))


def observe_approval(kind: str, decision: str) -> None:
    APPROVALS.labels(kind=kind, decision=decision).inc()


def observe_delivery_attempt(attempt_type: str) -> None:
    DELIVERY_ATTEMPTS.labels(attempt_type=attempt_type).inc()


def observe_delivery_outcome(outcome: str) -> None:
    DELIVERY_OUTCOMES.labels(outcome=outcome).inc()
