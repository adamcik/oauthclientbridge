"""Compatibility layer for unstable OpenTelemetry imports and test helpers.

This module intentionally centralizes private/unstable OTel usage in one place,
so the rest of the codebase can import from here and avoid scattering
version-specific paths.
"""

import opentelemetry.metrics._internal
import opentelemetry.trace
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.semconv._incubating.attributes.http_attributes import (
    HTTP_REQUEST_BODY_SIZE,
    HTTP_RESPONSE_BODY_SIZE,
)
from opentelemetry.util._once import Once

__all__ = [
    "HTTP_REQUEST_BODY_SIZE",
    "HTTP_RESPONSE_BODY_SIZE",
    "InMemoryLogRecordExporter",
    "InMemoryMetricReader",
    "InMemorySpanExporter",
    "SimpleLogRecordProcessor",
    "reset_otel_once",
]


def reset_otel_once() -> None:
    # WARNING: This function directly manipulates OpenTelemetry's internal
    # `_Once` objects. OpenTelemetry SDK providers (TracerProvider,
    # MeterProvider, etc.) are designed to be singletons per process, meaning
    # `set_tracer_provider()` can only be called once per Python process.
    #
    # In a testing environment, especially when running tests sequentially
    # within the same process, this singleton behavior prevents re-initializing
    # providers for subsequent tests.
    #
    # This hack resets internal flags that track whether providers were set,
    # allowing tracer/meter providers to be initialized again in the same
    # process during tests.
    #
    # Alternatives considered:
    # - Using `pytest-xdist` or `pytest-isolate`: While these provide process
    #   isolation, they don't guarantee a fresh process for every single test
    #   without complex (and sometimes problematic) configuration. If a worker
    #   process is reused, the global OpenTelemetry state persists, leading to
    #   "Overriding" errors.
    #
    # Given the OpenTelemetry SDK's design and the need for reliable test
    # isolation without excessive overhead, directly resetting these internal
    # flags is currently the most pragmatic and least intrusive solution for
    # ensuring a clean OpenTelemetry state before each test.
    opentelemetry.trace._TRACER_PROVIDER_SET_ONCE = Once()
    opentelemetry.metrics._internal._METER_PROVIDER_SET_ONCE = Once()
