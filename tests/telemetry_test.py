from typing import Generator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from oauthclientbridge.settings import (
from oauthclientbridge.telemetry import init_tracing
    TelemetryComponent,
    TelemetrySettings,
)

tracer = trace.get_tracer(__name__)


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

    yield exporter

    processor.shutdown()


def test_telemetry_tracer_otel_enabled(captraces: InMemorySpanExporter) -> None:
    with tracer.start_as_current_span("test-span-1"):
        with tracer.start_as_current_span("test-span-2"):
            pass

    finished_spans = captraces.get_finished_spans()
    assert len(finished_spans) == 2
    assert "test-span-1" in [span.name for span in finished_spans]
    assert "test-span-2" in [span.name for span in finished_spans]
