import pytest

from oauthclientbridge import telemetry
from tests.plugins.otel.helpers import CollectedLog

from . import OTelMocker


def test_otel_mock_fixture_initializes_telemetry(monkeypatch, request) -> None:
    calls: dict[str, tuple[object, object]] = {}

    def fake_init_metrics(settings, metric_reader) -> None:
        calls["metrics"] = (settings, metric_reader)

    def fake_init_tracing(settings, span_processor) -> None:
        calls["tracing"] = (settings, span_processor)

    monkeypatch.setattr(telemetry, "init_metrics", fake_init_metrics)
    monkeypatch.setattr(telemetry, "init_tracing", fake_init_tracing)

    otel_mock = request.getfixturevalue("otel_mock")

    assert isinstance(otel_mock, OTelMocker)
    assert calls["metrics"][1] is otel_mock.metric_reader
    assert calls["tracing"][1] is otel_mock.span_processor


def test_otel_plugin_exposes_tracer_and_meter(request) -> None:
    tracer = request.getfixturevalue("tracer")
    meter = request.getfixturevalue("meter")

    assert tracer is not None
    assert meter is not None


@pytest.fixture
def telemetry_instrument_calls(monkeypatch):
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
    telemetry_instrument_calls, instrumented
) -> None:
    _ = instrumented
    assert telemetry_instrument_calls == ["instrument"]


def test_collected_log_uses_structural_typing_for_new_otel_versions() -> None:
    class FakeRecord:
        resource = object()
        attributes = {"k": "v"}
        body = "hello"

        def to_json(self) -> str:
            return "{}"

    class FakeLogData:
        instrumentation_scope = object()
        resource = object()
        log_record = FakeRecord()

    collected = CollectedLog(FakeLogData())

    assert collected.attributes == {"k": "v"}
    assert collected.body == "hello"
    assert str(collected) == "{}"
