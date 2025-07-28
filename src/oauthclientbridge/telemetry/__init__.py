import importlib.util
from typing import NoReturn

import structlog

from oauthclientbridge.settings import OtelSettings

logger: structlog.BoundLogger = structlog.get_logger()

_otel_available = bool(importlib.util.find_spec("opentelemetry"))


def _assert_never(value: NoReturn) -> NoReturn:
    raise AssertionError(f"Unhandled type: {value} ({type(value).__name__})")


def init(settings: OtelSettings) -> None:
    if not settings.enabled:
        return

    if not _otel_available:
        logger.error(
            "OpenTelemetry is enabled, but its dependencies are not installed. "
            "Please install them with 'pip install oauthclientbridge[opentelemetry]'."
        )
        return

    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
    from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor

    from oauthclientbridge.telemetry.metrics import init_metrics
    from oauthclientbridge.telemetry.traces import init_traces

    init_traces(settings)
    init_metrics()

    FlaskInstrumentor().instrument()
    RequestsInstrumentor().instrument()
    SQLite3Instrumentor().instrument()
    LoggingInstrumentor().instrument()
    SystemMetricsInstrumentor().instrument()
