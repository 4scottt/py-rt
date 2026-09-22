"""OpenTelemetry wiring: metrics and traces, a no-op without ``OTEL_*`` (plan
§6, O03/O04).

Only :func:`configure` looks at the environment here, and only
``OTEL_EXPORTER_OTLP_ENDPOINT`` (to decide the no-op) and
``OTEL_SERVICE_NAME`` (the resource's default); the rest of the standard
``OTEL_*`` names (``OTEL_EXPORTER_OTLP_PROTOCOL``, ``OTEL_RESOURCE_ATTRIBUTES``,
``OTEL_METRIC_EXPORT_INTERVAL``, …) are read by the SDK and the exporters
themselves. No new :class:`~pyrt.config.Settings` field: the plan says the
app sets ``OTEL_SEMCONV_STABILITY_OPT_IN`` itself and otherwise leaves the
rest to the SDK, never the card.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final

from fastapi import FastAPI
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from sqlalchemy import Engine

from pyrt.config import Settings

log = logging.getLogger(__name__)

#: The instrument the stable HTTP semconv opt-in yields, verified against
#: opentelemetry-instrumentation-fastapi/-asgi 0.65b0: with
#: ``OTEL_SEMCONV_STABILITY_OPT_IN=http`` (mode ``HTTP``, not ``HTTP_DUP``)
#: ``ASGIGetterGetter``'s ``_report_old`` is ``False`` and ``_report_new`` is
#: ``True``, so the *only* histogram created is this one, unit ``s``; the old
#: ``http.server.duration`` (ms) is not also emitted. If a future
#: instrumentation version changes the name or unit, O04 fails loudly on it
#: rather than silently passing on the wrong instrument.
HTTP_SERVER_REQUEST_DURATION: Final = "http.server.request.duration"

#: The attributes the plan keeps on that instrument (plan §6: "route,
#: method, status"). The instrumentation's stable attribute set for this
#: histogram also carries ``error.type``, ``network.protocol.version`` and
#: ``url.scheme``; the View below drops them at collection.
_KEPT_HTTP_ATTRS: Final = frozenset(
    {"http.request.method", "http.response.status_code", "http.route"}
)

#: opentelemetry-exporter-otlp-proto-http's two exporters and the SDK's own
#: export machinery: with no collector, a connection refused is *retryable*
#: (``opentelemetry/exporter/otlp/proto/http/{metric,trace}_exporter``'s
#: ``_export_serialized_data`` treats ``requests.exceptions.ConnectionError``
#: that way), so every attempt and the final give-up both ``_logger.warning``
#: or ``.error`` — and ``BatchSpanProcessor``/``PeriodicExportingMetricReader``
#: warn on their own retries too. The plan: "logged at debug, never fatal";
#: since none of these call sites can be told to log at DEBUG instead, a
#: filter on these four logger names demotes their records to DEBUG, so a
#: deploy's ~30s gap before the sidecar answers stays quiet at INFO and a
#: wrong endpoint still shows under ``--log-level debug``.
_QUIET_LOGGERS: Final = (
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.exporter.otlp.proto.http.metric_exporter",
    "opentelemetry.sdk.metrics._internal.export",
    "opentelemetry.sdk.trace.export",
)


class Unset(Enum):
    """A typed sentinel distinct from ``None`` (an explicit "no telemetry").

    :func:`pyrt.app.create_app`'s ``telemetry`` parameter needs three states:
    not given at all (build real telemetry from the environment, the serve
    path), given as ``None`` (a caller-built no-op), and given as a
    :class:`Telemetry` (a caller-built seam, O04's in-memory readers).
    """

    TOKEN = auto()


UNSET: Final = Unset.TOKEN


@dataclass(frozen=True, slots=True)
class Telemetry:
    """The two providers :func:`instrument_app`, :func:`instrument_engine`
    and :func:`shutdown` need."""

    tracer_provider: TracerProvider
    meter_provider: MeterProvider


class _DemoteToDebug(logging.Filter):
    """The plan's "logged at debug": the SDK's call sites cannot be told to
    log lower, so their records are rewritten to DEBUG on the way out. They
    still show under ``--log-level debug`` (a wrong endpoint is then
    visible), and never at INFO or above."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.DEBUG:
            record.levelno, record.levelname = logging.DEBUG, "DEBUG"
        return True


_DEMOTER: Final = _DemoteToDebug()


def _quiet_exporter_loggers() -> None:
    """Demote the four names in :data:`_QUIET_LOGGERS` to DEBUG."""
    for name in _QUIET_LOGGERS:
        logger = logging.getLogger(name)
        if _DEMOTER not in logger.filters:
            logger.addFilter(_DEMOTER)


def configure(
    settings: Settings,
    *,
    metric_readers: list[MetricReader] | None = None,
    span_exporter: SpanExporter | None = None,
    set_global: bool = False,
) -> Telemetry | None:
    """Build the tracer and meter providers, or return ``None`` (O03).

    With no ``OTEL_EXPORTER_OTLP_ENDPOINT`` set and no test seam
    (``metric_readers``/``span_exporter``) passed, this creates no provider,
    exporter or instrumentation at all — a true no-op, not a disabled one.

    ``set_global`` installs the SDK's *global* tracer/meter provider (the SDK
    allows exactly one set per process); only the serve path
    (:func:`pyrt.app.create_app`'s default, no ``telemetry=`` seam) passes
    ``True``. Tests always pass seams and never set the globals.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint and metric_readers is None and span_exporter is None:
        return None

    # The instrumentations read OTEL_SEMCONV_STABILITY_OPT_IN lazily, once
    # per process, on the first .instrument() call (a one-shot in
    # opentelemetry.instrumentation._semconv), so it must be set before that
    # first call — every time, here, not by the card.
    os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "http"

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.metrics.view import View
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    _quiet_exporter_loggers()

    # Resource.create() reads OTEL_RESOURCE_ATTRIBUTES itself; the explicit
    # service.name here (from OTEL_SERVICE_NAME, defaulted) is read the same
    # way the SDK's own detector would read it, so it is correct whichever
    # way OTEL_SERVICE_NAME is or isn't set (an attribute passed to create()
    # always wins the merge, so reading the env var ourselves for the default
    # avoids stepping on a value the SDK would otherwise have supplied).
    service_name = os.environ.get("OTEL_SERVICE_NAME", "").strip() or "py-rt"
    resource = Resource.create({SERVICE_NAME: service_name})

    view = View(instrument_name=HTTP_SERVER_REQUEST_DURATION, attribute_keys=set(_KEPT_HTTP_ATTRS))
    readers = (
        metric_readers
        if metric_readers is not None
        else [PeriodicExportingMetricReader(OTLPMetricExporter())]
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=readers, views=[view])

    tracer_provider = TracerProvider(resource=resource)
    processor = (
        SimpleSpanProcessor(span_exporter)
        if span_exporter is not None
        else BatchSpanProcessor(OTLPSpanExporter())
    )
    tracer_provider.add_span_processor(processor)

    if set_global:
        from opentelemetry.metrics import set_meter_provider
        from opentelemetry.trace import set_tracer_provider

        set_meter_provider(meter_provider)
        set_tracer_provider(tracer_provider)

    return Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider)


def instrument_app(app: FastAPI, telemetry: Telemetry) -> None:
    """Wrap ``app`` in the FastAPI instrumentation.

    ``excluded_urls="health"`` keeps the platform's probe (plan §6: no row
    written by a probe) out of the metrics and traces too.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=r"^https?://[^/]+/health$",
        tracer_provider=telemetry.tracer_provider,
        meter_provider=telemetry.meter_provider,
    )


def instrument_engine(engine: Engine, telemetry: Telemetry) -> None:
    """One client span per query; the sqlcommenter stays off (the
    instrumentation's own default, unchanged here).

    The instrumentor wraps the engine given *and* patches SQLAlchemy's
    engine constructors and ``Engine.connect`` process-wide, and its
    ``uninstrument`` strips the listeners from every engine instrumented so
    far: two apps in one process cannot each keep their own (the serve path
    builds one app per worker process, so it never meets this).

    ``SQLAlchemyInstrumentor`` is a process-wide singleton
    (``BaseInstrumentor.__new__`` always returns the same instance): once
    instrumented, a second ``.instrument(engine=...)`` call is a silent
    no-op (it warns and returns ``None`` without wrapping the new engine),
    which would leave a later app's engine dark. Tests build many apps
    (each ``create_app()`` call gets its own engine), so an
    already-instrumented instrumentor is uninstrumented first and
    re-instrumented against the new engine and provider.
    """
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    instrumentor = SQLAlchemyInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    instrumentor.instrument(engine=engine, tracer_provider=telemetry.tracer_provider)


def shutdown(telemetry: Telemetry) -> None:
    """Flush and stop both providers so no export thread is left running."""
    telemetry.tracer_provider.shutdown()
    telemetry.meter_provider.shutdown()


def uninstrument_engine() -> None:
    """Undo :func:`instrument_engine` for the process (tests)."""
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    instrumentor = SQLAlchemyInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
