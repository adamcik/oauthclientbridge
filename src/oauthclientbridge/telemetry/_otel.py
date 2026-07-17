import importlib.util
from collections.abc import Mapping
from typing import Any, assert_never
from urllib.parse import urlsplit
from wsgiref.util import request_uri

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

# Import the leaf module directly; importing through telemetry's facade creates a cycle.
import oauthclientbridge.telemetry.sentry as sentry
from oauthclientbridge import types
from oauthclientbridge.settings import (
    TelemetryComponent,
    TelemetryExporter,
    TelemetrySettings,
)
from oauthclientbridge.utils import uri

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


def _flask_response_hook(
    span: trace.Span,
    status: str,
    headers: Mapping[str, str] | list[tuple[str, str]],
) -> None:
    if not span or not span.is_recording():
        return

    headers = CaseInsensitiveDict[str](headers)
    location = headers.get("Location")
    if location is not None:
        sanitized_location = uri.sanitize_url(location)
        if sanitized_location is not None:
            span.set_attribute(
                "http.response.header.location",
                sanitized_location,
            )
    cache_control = headers.get("Cache-Control")
    if cache_control is not None:
        span.set_attribute(
            "http.response.header.cache_control",
            cache_control,
        )
    content_type = headers.get("Content-Type")
    if content_type is not None:
        span.set_attribute(
            "http.response.header.content_type",
            content_type,
        )
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        span.set_attribute(
            "http.response.header.retry_after",
            retry_after,
        )


def _flask_request_hook(span: trace.Span, environ: dict[str, Any]) -> None:
    if not span or not span.is_recording():
        return

    sanitized_url = uri.sanitize_url(request_uri(environ))
    sanitized_url_parts = urlsplit(sanitized_url) if sanitized_url else None
    if sanitized_url is not None:
        # Flask instrumentation records this legacy attribute before invoking hooks.
        span.set_attribute("http.url", sanitized_url)
        span.set_attribute("url.full", sanitized_url)
    if sanitized_url_parts is not None:
        span.set_attribute("url.query", sanitized_url_parts.query)


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
        request_hook=_flask_request_hook,
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
