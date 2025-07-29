import pytest
from pydantic import ValidationError

from oauthclientbridge.settings import OtelExporterProtocol, TelemetrySettings


def test_otel_settings_defaults() -> None:
    settings = TelemetrySettings()
    assert settings.enabled is False
    assert settings.exporter_protocol == OtelExporterProtocol.OTLP_GRPC
    assert settings.endpoint == "http://localhost:4317"
    assert settings.service_name == "oauthclientbridge"


def test_otel_settings_otlp_grpc_no_endpoint_raises_error() -> None:
    with pytest.raises(ValidationError) as excinfo:
        TelemetrySettings(
            enabled=True,
            exporter_protocol=OtelExporterProtocol.OTLP_GRPC,
            endpoint=None,
        )
    assert "OTEL_ENDPOINT must be set if OTEL_EXPORTER_PROTOCOL is OTLP_GRPC" in str(
        excinfo.value
    )


def test_otel_settings_otlp_grpc_with_endpoint() -> None:
    settings = TelemetrySettings(
        enabled=True,
        exporter_protocol=OtelExporterProtocol.OTLP_GRPC,
        endpoint="http://my-collector:4317",
    )
    assert settings.enabled is True
    assert settings.exporter_protocol == OtelExporterProtocol.OTLP_GRPC
    assert settings.endpoint == "http://my-collector:4317"


def test_otel_settings_custom_service_name() -> None:
    settings = TelemetrySettings(service_name="my-custom-service")
    assert settings.service_name == "my-custom-service"
