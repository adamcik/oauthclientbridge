import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from structlog.testing import capture_logs

from oauthclientbridge import telemetry
from oauthclientbridge.settings import OtelSettings
from oauthclientbridge.telemetry import traces


@pytest.fixture
def in_memory_exporter():
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    yield exporter, processor
    exporter.clear()
    traces.shutdown_traces()


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


def test_init_traces_with_in_memory_exporter(
    in_memory_exporter: tuple[InMemorySpanExporter, SimpleSpanProcessor],
) -> None:
    exporter, processor = in_memory_exporter
    settings = OtelSettings(
        enabled=True, exporter_protocol=None
    )  # Protocol doesn't matter here as we pass the exporter directly
    traces.init_traces(settings, span_processor=processor)

    # Generate a span and verify it's captured by the in-memory exporter
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("test-span"):
        pass

    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 1
    assert finished_spans[0].name == "test-span"
