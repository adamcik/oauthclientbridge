import importlib.util
from typing import assert_never

import requests
import structlog
from flask import Flask
from opentelemetry import trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
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
from opentelemetry.sdk.metrics.view import (
    ExplicitBucketHistogramAggregation,
    View,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from requests.structures import CaseInsensitiveDict

from oauthclientbridge import sentry, types
from oauthclientbridge.settings import (
    TelemetryComponent,
    TelemetryExporter,
    TelemetrySettings,
)

from ._buckets import BYTES, TIME
from ._resources import otel_log_attributes, resource_attributes


def set_client_id(client_id: types.ClientId) -> None:
    """Associate a canonical client ID with the current request telemetry."""
    client_id_string = str(client_id)
    structlog.contextvars.bind_contextvars(client_id=client_id_string)
    trace.get_current_span().set_attribute("client_id", client_id_string)
    sentry.set_user({"client_id": client_id_string})


def record_invalid_client_id(client_id: str) -> None:
    """Preserve the rejected input without treating it as a client identity."""
    structlog.contextvars.bind_contextvars(invalid_client_id=client_id)
    trace.get_current_span().add_event("invalid_client_id", {"client_id": client_id})


def _requests_response_hook(
    span: trace.Span,
    request: requests.PreparedRequest,
    response: requests.Response,
):
    if not span or not span.is_recording():
        return

    span.set_attribute(
        "http.response.body.size",
        len(response.content),
    )

    if "Content-Length" in response.headers:
        span.set_attribute(
            "http.response.header.content_length",
            response.headers["Content-Length"],
        )
    if "Content-Type" in response.headers:
        span.set_attribute(
            "http.response.header.content_type",
            response.headers["Content-Type"],
        )
    if "Retry-After" in response.headers:
        span.set_attribute(
            "http.response.header.retry_after",
            response.headers["Retry-After"],
        )


def _flask_response_hook(span: trace.Span, status: str, headers):
    if not span or not span.is_recording():
        return

    headers = CaseInsensitiveDict(headers)
    if "Location" in headers:
        span.set_attribute(
            "http.response.header.location",
            headers["Location"],
        )
    if "Cache-Control" in headers:
        span.set_attribute(
            "http.response.header.cache_control",
            headers["Cache-Control"],
        )
    if "Content-Type" in headers:
        span.set_attribute(
            "http.response.header.content_type",
            headers["Content-Type"],
        )
    if "Retry-After" in headers:
        span.set_attribute(
            "http.response.header.retry_after",
            headers["Retry-After"],
        )


def _logging_log_hook(span: trace.Span, record: object):
    if not span or not span.is_recording():
        return

    setattr(record, "telemetry_attributes", otel_log_attributes(span))


instrumentors = [
    (SystemMetricsInstrumentor(), {}),
    (LoggingInstrumentor(), {"log_hook": _logging_log_hook}),
    (SQLite3Instrumentor(), {}),
    (RequestsInstrumentor(), {"response_hook": _requests_response_hook}),
]


def instrument():
    for inst, kwargs in instrumentors:
        inst.instrument(**kwargs)


def uninstrument():
    for inst, _ in instrumentors:
        inst.uninstrument()


def instrument_app(app: Flask) -> None:
    # TODO: See if we can do this with global flask instrument without breaking
    # server or tests.
    FlaskInstrumentor().instrument_app(
        app,
        response_hook=_flask_response_hook,
    )


def init_tracing(
    settings: TelemetrySettings,
    span_processor: SpanProcessor | None = None,
) -> None:
    if TelemetryComponent.TRACING not in settings.components:
        return

    provider = TracerProvider(resource=Resource.create(resource_attributes(settings)))

    if span_processor:
        provider.add_span_processor(span_processor)

    for exporter_protocol in settings.exporters:
        match exporter_protocol:
            case TelemetryExporter.OTLP_HTTP:
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

    resource = Resource.create(resource_attributes(settings))

    readers: list[MetricReader] = []
    if metric_reader:
        readers.append(metric_reader)

    for exporter_protocol in settings.exporters:
        match exporter_protocol:
            case TelemetryExporter.OTLP_HTTP:
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
            views=[
                View(
                    instrument_name="http.*.duration",
                    aggregation=ExplicitBucketHistogramAggregation(boundaries=TIME),
                ),
                View(
                    instrument_name="http.*.size",
                    aggregation=ExplicitBucketHistogramAggregation(boundaries=BYTES),
                ),
                View(
                    instrument_name="oauth.db.cursor.duration",
                    aggregation=ExplicitBucketHistogramAggregation(boundaries=TIME),
                    attribute_keys={"db.operation", "error.type"},
                ),
                View(
                    instrument_name="oauth.client.duration",
                    aggregation=ExplicitBucketHistogramAggregation(boundaries=TIME),
                    attribute_keys={"operation", "final.result", "error.type"},
                ),
                View(
                    instrument_name="oauth.client.retries",
                    attribute_keys={"operation", "final.result", "error.type"},
                ),
            ],
        )
    )
