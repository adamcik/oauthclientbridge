from typing import Generator

import pytest
from opentelemetry import metrics, trace

from oauthclientbridge import telemetry
from oauthclientbridge.settings import TelemetryComponent, TelemetrySettings
from oauthclientbridge.testing.otel import (
    InMemoryLogRecordExporter,
    InMemoryMetricReader,
    InMemorySpanExporter,
)

from .mocker import OTelMocker


@pytest.fixture(name="otel_mock")
def fixture_otel_mock() -> Generator[OTelMocker, None, None]:
    settings = TelemetrySettings(
        components={
            TelemetryComponent.TRACING,
            TelemetryComponent.METRICS,
        },
        service_name="tests",
        service_namespace="oauthclientbridge",
        service_version="1.2.3",
        deployment_environment="testing",
        oauth_provider="spotify",
        service_instance_id="tests-spotify-testing",
        vcs_revision="abc1234",
    )

    log_exporter = InMemoryLogRecordExporter()
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    with OTelMocker(log_exporter, span_exporter, metric_reader) as mocker:
        telemetry.init_metrics(settings, metric_reader)
        telemetry.init_tracing(settings, mocker.span_processor)
        yield mocker


@pytest.fixture(name="tracer")
def fixture_tracer(otel_mock: OTelMocker):
    _ = otel_mock
    return trace.get_tracer("tests")


@pytest.fixture(name="meter")
def fixture_meter(otel_mock: OTelMocker):
    _ = otel_mock
    return metrics.get_meter("tests")


@pytest.fixture
def instrumented(otel_mock: OTelMocker):
    _ = otel_mock
    telemetry.instrument()
    try:
        yield
    finally:
        telemetry.uninstrument()
