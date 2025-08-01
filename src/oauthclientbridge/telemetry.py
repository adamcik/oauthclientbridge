import contextlib
import importlib.util
from typing import assert_never

from flask import Flask
from opentelemetry import trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.propagators import (
    TraceResponsePropagator,
    set_global_response_propagator,
)
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
from opentelemetry.instrumentation.system_metrics import (
    SystemMetricsInstrumentor,
)
from opentelemetry.metrics import set_meter_provider
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import TextMapPropagator
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from oauthclientbridge.settings import (
    TelemetryComponent,
    TelemetryExporter,
    TelemetrySettings,
)


@contextlib.contextmanager
def instrument():
    instrumentors = [
        SQLite3Instrumentor(),
        RequestsInstrumentor(),
        SystemMetricsInstrumentor(),
        LoggingInstrumentor(),
    ]

    for inst in instrumentors:
        inst.instrument()

    try:
        yield
    finally:
        for inst in instrumentors:
            inst.uninstrument()


def instrument_app(app: Flask) -> None:
    # TODO: See if we can do this with global flask instrument without breaking
    # server or tests.
    FlaskInstrumentor().instrument_app(app)


def init_tracing(
    settings: TelemetrySettings,
    span_processor: SpanProcessor | None = None,
) -> None:
    if TelemetryComponent.TRACING not in settings.components:
        return
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
                assert_never(exporter_protocol)

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


def init_metrics(
    settings: TelemetrySettings,
    metric_reader: MetricReader | None = None,
) -> None:
    if TelemetryComponent.METRICS not in settings.components:
        return

    resource = Resource.create({SERVICE_NAME: settings.service_name})

    readers: list[MetricReader] = []
    if metric_reader:
        readers.append(metric_reader)

    for exporter_protocol in settings.exporters:
        match exporter_protocol:
            case TelemetryExporter.OTLP_GRPC:
                exporter = OTLPMetricExporter(endpoint=settings.endpoint)
                readers.append(
                    PeriodicExportingMetricReader(
                        exporter,
                        export_interval_millis=int(
                            settings.metric_export_interval_seconds * 1000
                        ),
                    )
                )
            case TelemetryExporter.CONSOLE:
                exporter = ConsoleMetricExporter()
                readers.append(
                    PeriodicExportingMetricReader(
                        exporter,
                        export_interval_millis=int(
                            settings.metric_export_interval_seconds * 1000
                        ),
                    )
                )
            case _:
                assert_never(exporter_protocol)

    set_meter_provider(
        MeterProvider(
            resource=resource,
            metric_readers=readers,
        )
    )
