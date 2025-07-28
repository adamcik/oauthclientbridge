import importlib.util
from typing import Any

import structlog

from oauthclientbridge.settings import OtelSettings
from oauthclientbridge.telemetry.traces import NoOpTracer, Tracer

logger: structlog.BoundLogger = structlog.get_logger()

tracer: Tracer = NoOpTracer()

_otel_available: bool = bool(importlib.util.find_spec("opentelemetry"))
_sentry_available: bool = bool(importlib.util.find_spec("sentry_sdk"))


def init(
    settings: OtelSettings,
    sentry_enabled: bool = False,
    span_processor: Any | None = None,
) -> None:
    if settings.enabled and not _otel_available:
        logger.error(
            "OpenTelemetry is enabled, but its dependencies are not installed. "
            "Please install them with 'pip install oauthclientbridge[opentelemetry]'."
        )

    global tracer
    tracer = _select_tracer(settings, sentry_enabled)

    if not settings.enabled or not _otel_available:
        return

    from oauthclientbridge.telemetry.metrics import init_metrics
    from oauthclientbridge.telemetry.traces import init_traces

    init_traces(settings, span_processor=span_processor)
    init_metrics()
    _init_instrumentation()


def _select_tracer(settings: OtelSettings, sentry_enabled: bool) -> Tracer:
    if settings.enabled and _otel_available:
        from oauthclientbridge.telemetry.traces import OtelTracer

        return OtelTracer()
    elif sentry_enabled and _sentry_available:
        from oauthclientbridge.sentry import SentryTracer

        return SentryTracer()
    else:
        return NoOpTracer()


# TODO: Move this to a separate module instrumentation
def _init_instrumentation() -> None:
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
    from opentelemetry.instrumentation.system_metrics import (
        SystemMetricsInstrumentor,
    )

    FlaskInstrumentor().instrument()
    RequestsInstrumentor().instrument()
    SQLite3Instrumentor().instrument()
    LoggingInstrumentor().instrument()
    SystemMetricsInstrumentor().instrument()
