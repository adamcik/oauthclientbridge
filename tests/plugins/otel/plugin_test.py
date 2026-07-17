from collections.abc import Generator
from typing import Any, Mapping, cast

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from oauthclientbridge import telemetry
from oauthclientbridge.settings import TelemetrySettings
from pytest_otel_capture import CollectedLog, OTelMocker


def test_otel_mock_fixture_initializes_telemetry(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    calls: dict[str, tuple[object, object]] = {}

    def fake_init_metrics(
        settings: TelemetrySettings, metric_reader: MetricReader | None
    ) -> None:
        calls["metrics"] = (settings, metric_reader)

    def fake_init_tracing(
        settings: TelemetrySettings, span_processor: SpanProcessor | None
    ) -> None:
        calls["tracing"] = (settings, span_processor)

    monkeypatch.setattr(telemetry, "init_metrics", fake_init_metrics)
    monkeypatch.setattr(telemetry, "init_tracing", fake_init_tracing)

    otel_mock = cast(OTelMocker, request.getfixturevalue("otel_mock"))

    assert isinstance(otel_mock, OTelMocker)
    assert calls["metrics"][1] is otel_mock.metric_reader
    assert calls["tracing"][1] is otel_mock.span_processor


def test_otel_plugin_exposes_tracer_and_meter(request: pytest.FixtureRequest) -> None:
    tracer = cast(trace.Tracer, request.getfixturevalue("tracer"))
    meter = cast(metrics.Meter, request.getfixturevalue("meter"))

    assert tracer is not None
    assert meter is not None


@pytest.fixture
def telemetry_instrument_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[list[str], None, None]:
    calls: list[str] = []

    def fake_instrument() -> None:
        calls.append("instrument")

    def fake_uninstrument() -> None:
        calls.append("uninstrument")

    monkeypatch.setattr(telemetry, "instrument", fake_instrument)
    monkeypatch.setattr(telemetry, "uninstrument", fake_uninstrument)

    yield calls

    assert calls == ["instrument", "uninstrument"]


def test_instrumented_fixture_wraps_test(
    telemetry_instrument_calls: list[str], instrumented: None
) -> None:
    _ = instrumented
    assert telemetry_instrument_calls == ["instrument"]


def test_collected_log_uses_structural_typing_for_new_otel_versions() -> None:
    class FakeRecord:
        attributes: Mapping[str, Any] | None = {"k": "v"}
        body: Any = "hello"

        def to_json(self) -> str:
            return "{}"

    class FakeLogData:
        instrumentation_scope: InstrumentationScope | None = InstrumentationScope(
            "test"
        )
        resource: Resource = Resource.create()
        log_record: FakeRecord = FakeRecord()

    collected = CollectedLog(FakeLogData())

    assert collected.attributes == {"k": "v"}
    assert collected.body == "hello"
    assert str(collected) == "{}"
