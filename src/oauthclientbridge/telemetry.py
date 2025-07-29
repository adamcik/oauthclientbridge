import importlib.util
from typing import NoReturn

from flask import Flask
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
from opentelemetry.instrumentation.system_metrics import (
    SystemMetricsInstrumentor,
)
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)

from oauthclientbridge.settings import OtelExporterProtocol, TelemetrySettings

_flask_instrumentor = FlaskInstrumentor()
_requests_instrumentor = RequestsInstrumentor()
_sqlite3_instrumentor = SQLite3Instrumentor()
_system_metrics_instrumentor = SystemMetricsInstrumentor()


def init_tracing(
    settings: TelemetrySettings,
    span_processor: SpanProcessor | None = None,
) -> None:
    if span_processor is None:
        match settings.exporter:
            case OtelExporterProtocol.OTLP_GRPC:
                span_processor = BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=settings.endpoint)
                )
            case OtelExporterProtocol.CONSOLE:
                span_processor = BatchSpanProcessor(ConsoleSpanExporter())
            case None:
                span_processor = None
            case _:
                _assert_never(settings.exporter)

    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: settings.service_name})
    )

    if span_processor:
        provider.add_span_processor(span_processor)

    if importlib.util.find_spec("sentry_sdk"):
        from sentry_sdk.integrations.opentelemetry.propagator import SentryPropagator
        from sentry_sdk.integrations.opentelemetry.span_processor import (
            SentrySpanProcessor,
        )

        provider.add_span_processor(SentrySpanProcessor())
        set_global_textmap(SentryPropagator())

    trace.set_tracer_provider(provider)


def instrument() -> None:
    _requests_instrumentor.instrument()
    _sqlite3_instrumentor.instrument()
    _system_metrics_instrumentor.instrument()

    # TODO: See how this would interact with structlog
    # _logging_instrumentor.instrument()


def instrument_app(app: Flask) -> None:
    _flask_instrumentor.instrument_app(app)


def _assert_never(value: NoReturn) -> NoReturn:
    raise AssertionError(f"Unhandled type: {value} ({type(value).__name__})")
