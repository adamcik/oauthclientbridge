from typing import Generator, cast

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricsData,
    NumberDataPoint,
)
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from oauthclientbridge.settings import (
    TelemetryComponent,
    TelemetrySettings,
)
from oauthclientbridge.telemetry import init_metrics, init_tracing

tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)


@pytest.fixture
def captraces() -> Generator[InMemorySpanExporter, None, None]:
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)

    init_tracing(
        TelemetrySettings(
            components={TelemetryComponent.TRACING},
        ),
        span_processor=processor,
    )

    try:
        yield exporter
    finally:
        processor.shutdown()


@pytest.fixture
def capmetrics() -> Generator[InMemoryMetricReader, None, None]:
    reader = InMemoryMetricReader()

    init_metrics(
        TelemetrySettings(
            components={TelemetryComponent.METRICS},
        ),
        metric_reader=reader,
    )

    try:
        yield reader
    finally:
        reader.shutdown()


def test_telemetry_tracer_otel_enabled(captraces: InMemorySpanExporter) -> None:
    with tracer.start_as_current_span("test-span-1"):
        with tracer.start_as_current_span("test-span-2"):
            pass

    finished_spans = captraces.get_finished_spans()
    assert len(finished_spans) == 2
    assert "test-span-1" in [span.name for span in finished_spans]
    assert "test-span-2" in [span.name for span in finished_spans]


def test_telemetry_metrics_otel_enabled(capmetrics: InMemoryMetricReader) -> None:
    counter = meter.create_counter("test_counter")
    counter.add(1)

    metrics_data = capmetrics.get_metrics_data()
    assert metrics_data is not None

    metrics_data = cast(MetricsData, metrics_data)
    assert len(metrics_data.resource_metrics) == 1
    assert len(metrics_data.resource_metrics[0].scope_metrics) == 1
    assert len(metrics_data.resource_metrics[0].scope_metrics[0].metrics) == 1

    metric = metrics_data.resource_metrics[0].scope_metrics[0].metrics[0]
    assert metric.name == "test_counter"

    data_point = metric.data.data_points[0]
    assert isinstance(data_point, NumberDataPoint)
    assert data_point.value == 1
