import pytest
from pydantic import ValidationError

from oauthclientbridge.settings import TelemetryExporter, TelemetrySettings


def test_telemetry_settings_defaults() -> None:
    settings = TelemetrySettings()
    assert settings.exporters == set()
    assert settings.endpoint == "http://localhost:4317"
    assert settings.service_name == "oauthclientbridge"


def test_telemetry_settings_otlp_grpc_no_endpoint_raises_error() -> None:
    with pytest.raises(ValidationError) as excinfo:
        TelemetrySettings(
            exporters={TelemetryExporter.OTLP_GRPC},
            endpoint=None,
        )
    assert "OTEL_ENDPOINT must be set if OTLP_GRPC is in TELEMETRY_EXPORTERS" in str(
        excinfo.value
    )


def test_telemetry_settings_otlp_grpc_with_endpoint() -> None:
    settings = TelemetrySettings(
        exporters={TelemetryExporter.OTLP_GRPC},
        endpoint="http://my-collector:4317",
    )
    assert settings.exporters == {TelemetryExporter.OTLP_GRPC}
    assert settings.endpoint == "http://my-collector:4317"


def test_telemetry_settings_custom_service_name() -> None:
    settings = TelemetrySettings(service_name="my-custom-service")
    assert settings.service_name == "my-custom-service"
