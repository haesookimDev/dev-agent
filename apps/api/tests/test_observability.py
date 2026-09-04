import json
import logging

import pytest
from httpx import AsyncClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from app import observability
from app.observability import (
    JsonFormatter,
    metrics_payload,
    observe_delivery_attempt,
    observe_delivery_outcome,
)


class MemoryExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans = []

    def export(self, spans) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


def test_json_logs_include_traceable_identifiers() -> None:
    record = logging.LogRecord(
        name="kelpie.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="work updated",
        args=(),
        exc_info=None,
    )
    record.work_id = "work-id"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "work updated"
    assert payload["work_id"] == "work-id"
    assert payload["request_id"]
    assert payload["correlation_id"]


@pytest.mark.asyncio
async def test_http_trace_contains_request_and_correlation_ids(
    client: AsyncClient,
    monkeypatch,
) -> None:
    exporter = MemoryExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        observability,
        "tracer",
        provider.get_tracer("kelpie.test"),
    )
    correlation_id = "44444444-4444-4444-8444-444444444444"

    response = await client.get(
        "/healthz",
        headers={"X-Kelpie-Correlation-ID": correlation_id},
    )

    assert response.status_code == 200
    span = exporter.spans[-1]
    assert span.attributes["http.request.method"] == "GET"
    assert span.attributes["http.response.status_code"] == 200
    assert span.attributes["kelpie.correlation_id"] == correlation_id
    assert span.attributes["kelpie.request_id"] == response.headers["X-Request-ID"]


def test_delivery_metrics_distinguish_retries_and_failures() -> None:
    observe_delivery_attempt("retry")
    observe_delivery_outcome("failed")

    payload = metrics_payload()[0].decode()

    assert 'kelpie_delivery_attempts_total{attempt_type="retry"}' in payload
    assert 'kelpie_delivery_outcomes_total{outcome="failed"}' in payload
