from typing import Generator, cast

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from structlog.testing import capture_logs

from oauthclientbridge import telemetry
from oauthclientbridge.settings import OtelSettings


@pytest.fixture
def captraces() -> Generator[InMemorySpanExporter, None, None]:
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)

    telemetry.init(
        OtelSettings(
            enabled=True,
            exporter_protocol=None,
        ),
        span_processor=processor,
    )

    yield exporter

    processor.shutdown()


def test_init_otel_disabled() -> None:
    telemetry.init(OtelSettings(enabled=False))
    # No assertions needed, just ensure no errors when disabled


def test_init_otel_enabled_no_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry, "_otel_available", False)

    with capture_logs() as logs:
        telemetry.init(OtelSettings(enabled=True))

    assert len(logs) == 1

    event = cast(str, logs[0].get("event"))
    assert "OpenTelemetry is enabled, but its dependencies are not installed." in event

    log_level = cast(str, logs[0].get("log_level"))
    assert log_level == "error"


def test_telemetry_tracer_otel_enabled(captraces: InMemorySpanExporter) -> None:
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
