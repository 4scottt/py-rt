"""OpenTelemetry: a no-op without ``OTEL_*``, real metrics and traces with it.

FP O03 (the no-op, and an absent collector logs quietly, never fatally) and
O04 (the stable HTTP metric and a SQLAlchemy client span, read back through
in-memory test readers, no network).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader, MetricsData
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from sqlalchemy.orm import Session

from pyrt import telemetry
from pyrt.app import create_app
from pyrt.config import Settings
from tests.conftest import TEST_DSN
from tests.test_auth import sign_in


def _histogram_points(metrics_data: MetricsData | None, name: str) -> list[HistogramDataPoint]:
    """Every data point of the histogram ``name``, across every resource and
    scope (there is one of each here, but this does not assume it)."""
    points: list[HistogramDataPoint] = []
    if metrics_data is None:
        return points
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    points.extend(
                        p for p in metric.data.data_points if isinstance(p, HistogramDataPoint)
                    )
    return points


def _client_spans(spans: Sequence[ReadableSpan]) -> list[ReadableSpan]:
    return [span for span in spans if span.kind == SpanKind.CLIENT]


def test_fp_o03_configure_is_a_true_no_op_without_otel_env_or_a_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``OTEL_EXPORTER_OTLP_ENDPOINT`` and no test seam: ``configure``
    returns ``None`` before creating any provider, exporter or
    instrumentation (O03)."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    settings = Settings(
        base_url="http://testserver", session_secret="test", database_url_override=TEST_DSN
    )
    assert telemetry.configure(settings) is None


def test_fp_o03_the_app_works_the_same_with_telemetry_off(client: TestClient) -> None:
    """The ordinary test app builds with no ``OTEL_*`` in its environment
    (``tests/conftest.py``'s ``client`` fixture): ``app.state.telemetry`` is
    ``None`` and every route still serves (O03)."""
    assert cast(FastAPI, client.app).state.telemetry is None
    sign_in(client)
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200


def test_fp_o03_an_absent_collector_logs_quietly_never_fatally(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``OTEL_EXPORTER_OTLP_ENDPOINT`` set to a closed port: the app still
    starts and serves ``/`` and ``/health``, and the SDK's retries and
    give-ups (on ``shutdown``, which forces a final export attempt) never
    reach the JSON log's INFO threshold (O03; plan §6, "logged at debug,
    never fatal"). ``OTEL_EXPORTER_OTLP_TIMEOUT`` is shortened so the
    export's own retry/backoff loop resolves in this test's lifetime rather
    than the exporters' 10s default.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "py-rt-o03-test")

    settings = Settings(
        base_url="http://testserver", session_secret="test", database_url_override=TEST_DSN
    )
    built = telemetry.configure(settings)
    assert built is not None, "an endpoint is set, so this must not be the no-op path"

    app = create_app(settings, telemetry=built)
    caplog.set_level(logging.DEBUG)
    try:
        with TestClient(app) as test_client:
            assert test_client.get("/").status_code == 200
            assert test_client.get("/health").status_code == 200
            time.sleep(2)
    finally:
        telemetry.shutdown(built)  # forces a final export attempt against the closed port
        telemetry.uninstrument_engine()
        app.state.engine.dispose()
        app.state.engine.dispose()

    noisy = [
        (record.name, record.levelname, record.getMessage())
        for record in caplog.records
        if record.name.startswith("opentelemetry") and record.levelno >= logging.INFO
    ]
    assert noisy == [], noisy


def test_fp_o04_a_request_and_a_query_are_recorded_in_memory(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-memory readers, no network: a request produces the stable
    ``http.server.request.duration`` histogram point (seconds, the kept
    attributes, the route templated) and a query produces a SQLAlchemy
    client span (O04)."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    settings = Settings(
        base_url="http://testserver", session_secret="test", database_url_override=TEST_DSN
    )
    metric_reader = InMemoryMetricReader()
    span_exporter = InMemorySpanExporter()
    built = telemetry.configure(
        settings, metric_readers=[metric_reader], span_exporter=span_exporter
    )
    assert built is not None, "a seam was passed, so this must not be the no-op path"

    app = create_app(settings, telemetry=built)
    try:
        with TestClient(app) as test_client:
            sign_in(test_client)
            missing = test_client.get("/ticket/1")
            assert missing.status_code == 404
            home = test_client.get("/")
            assert home.status_code == 200
    finally:
        telemetry.shutdown(built)
        telemetry.uninstrument_engine()
        app.state.engine.dispose()
        app.state.engine.dispose()

    metrics_data = metric_reader.get_metrics_data()
    points = _histogram_points(metrics_data, telemetry.HTTP_SERVER_REQUEST_DURATION)
    assert points, "no http.server.request.duration histogram point was recorded"

    ticket_points = [
        p for p in points if (p.attributes or {}).get("http.route") == "/ticket/{ticket_id}"
    ]
    assert ticket_points, [p.attributes for p in points]
    point = ticket_points[0]
    attributes = dict(point.attributes or {})
    assert attributes["http.request.method"] == "GET"
    assert attributes["http.response.status_code"] == 404
    # Only the kept attributes (plan §6): never the raw id, and none of the
    # stable set's error.type / network.protocol.version / url.scheme.
    assert set(attributes) == {
        "http.request.method",
        "http.response.status_code",
        "http.route",
    }
    assert point.count == 1
    assert 0 <= point.sum < 5

    finished_spans = span_exporter.get_finished_spans()
    server_spans = [s for s in finished_spans if s.kind == SpanKind.SERVER]
    assert server_spans, "no server span was recorded"

    client_spans = _client_spans(finished_spans)
    assert client_spans, "no SQLAlchemy client span was recorded"
    assert any("SELECT" in (span.name or "") for span in client_spans), [
        span.name for span in client_spans
    ]
