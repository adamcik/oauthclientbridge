import importlib.util
from typing import NoReturn

from flask import Flask
from opentelemetry import trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.propagators import (
    TraceResponsePropagator,
    set_global_response_propagator,
)
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
from opentelemetry.instrumentation.system_metrics import (
    SystemMetricsInstrumentor,
)
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import TextMapPropagator
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from oauthclientbridge.settings import (
    TelemetryExporter,
    TelemetrySettings,
)

_flask_instrumentor = FlaskInstrumentor()
_requests_instrumentor = RequestsInstrumentor()
_sqlite3_instrumentor = SQLite3Instrumentor()
_system_metrics_instrumentor = SystemMetricsInstrumentor()


def init_tracing(
    settings: TelemetrySettings,
    span_processor: SpanProcessor | None = None,
) -> None:
    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: settings.service_name})
    )

    if span_processor:
        provider.add_span_processor(span_processor)

    for exporter_protocol in settings.exporters:
        match exporter_protocol:
            case TelemetryExporter.OTLP_GRPC:
                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.endpoint))
                )
            case TelemetryExporter.CONSOLE:
                provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
            case _:
                _assert_never(exporter_protocol)

    propagators: list[TextMapPropagator] = [
        TraceContextTextMapPropagator(),
        W3CBaggagePropagator(),
    ]

    if importlib.util.find_spec("sentry_sdk"):
        from sentry_sdk.integrations.opentelemetry.propagator import SentryPropagator
        from sentry_sdk.integrations.opentelemetry.span_processor import (
            SentrySpanProcessor,
        )

        provider.add_span_processor(SentrySpanProcessor())
        propagators.append(SentryPropagator())

    trace.set_tracer_provider(provider)

    # Handle incoming and outgoing request headers
    set_global_textmap(CompositePropagator(propagators))

    # Set traceresponse header
    set_global_response_propagator(TraceResponsePropagator())


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
