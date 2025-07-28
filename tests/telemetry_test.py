from typing import Generator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from structlog.testing import capture_logs

from oauthclientbridge import telemetry
from oauthclientbridge.settings import OtelSettings


@pytest.fixture
def captraces() -> Generator[InMemorySpanExporter, None, None]:
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)

    settings = OtelSettings(enabled=True, exporter_protocol=None)
    telemetry.init(settings, span_processor=processor)

    yield exporter

    processor.shutdown()


def test_init_otel_disabled() -> None:
    settings = OtelSettings(enabled=False)
    telemetry.init(settings)
    # No assertions needed, just ensure no errors when disabled


def test_init_otel_enabled_no_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry, "_otel_available", False)
    settings = OtelSettings(enabled=True)
    with capture_logs() as logs:
        telemetry.init(settings)
    assert len(logs) == 1
    assert (
        logs[0].get("event")
        == "OpenTelemetry is enabled, but its dependencies are not installed. Please install them with 'pip install oauthclientbridge[opentelemetry]'."
    )
    assert logs[0].get("log_level") == "error"


def test_telemetry_tracer_otel_enabled(
    captraces: InMemorySpanExporter,
) -> None:
    with telemetry.tracer.start_transaction("test-transaction"):
        with telemetry.tracer.start_span("test-span-1"):
            pass
        with telemetry.tracer.start_span("test-span-2"):
            pass

    finished_spans = captraces.get_finished_spans()
    assert len(finished_spans) == 3  # transaction + 2 spans
    assert "test-transaction" in [span.name for span in finished_spans]
    assert "test-span-1" in [span.name for span in finished_spans]
    assert "test-span-2" in [span.name for span in finished_spans]


def test_init_traces_with_in_memory_exporter(
    captraces: InMemorySpanExporter,
) -> None:
    # This test now primarily verifies that init_traces can be called and spans are captured
    # The actual setup is handled by captraces fixture
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("test-span"):
        pass

    finished_spans = captraces.get_finished_spans()
    assert len(finished_spans) == 1
    assert finished_spans[0].name == "test-span"
