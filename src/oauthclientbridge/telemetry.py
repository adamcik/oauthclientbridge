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
    SpanExporter,
)

from oauthclientbridge.settings import OtelExporterProtocol, TelemetrySettings


def init_tracing(
    settings: TelemetrySettings, span_processor: SpanProcessor | None = None
) -> None:
    if not settings.enabled:
        return

    resource = Resource.create({SERVICE_NAME: settings.service_name})
    provider = TracerProvider(resource=resource)

    if span_processor is None:
        # TODO: Extract this into a helper function
        span_exporter: SpanExporter | None
        match settings.exporter_protocol:
            case OtelExporterProtocol.OTLP_GRPC:
                span_exporter = OTLPSpanExporter(endpoint=settings.endpoint)
            case OtelExporterProtocol.CONSOLE:
                span_exporter = ConsoleSpanExporter()
            case None:
                raise ValueError(
                    "TELEMETRY_EXPORTER_PROTOCOL must be set if no span_processor "
                    "is provided and no exporter protocol is set."
                )
            case _:
                _assert_never(settings.exporter_protocol)

        span_processor = BatchSpanProcessor(span_exporter)

    provider.add_span_processor(span_processor)

    if importlib.util.find_spec("sentry_sdk"):
        from sentry_sdk.integrations.opentelemetry.propagator import SentryPropagator
        from sentry_sdk.integrations.opentelemetry.span_processor import (
            SentrySpanProcessor,
        )

        provider.add_span_processor(SentrySpanProcessor())
        set_global_textmap(SentryPropagator())

    trace.set_tracer_provider(provider)


def init_instrumentation() -> None:
    RequestsInstrumentor().instrument()
    SQLite3Instrumentor().instrument()
    SystemMetricsInstrumentor().instrument()

    # TODO: See how this would interact with structlog
    # LoggingInstrumentor().instrument()


def instrument_app(app: Flask) -> None:
    FlaskInstrumentor().instrument_app(app)


def _assert_never(value: NoReturn) -> NoReturn:
    raise AssertionError(f"Unhandled type: {value} ({type(value).__name__})")
