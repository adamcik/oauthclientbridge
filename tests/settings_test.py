from pathlib import Path

import pytest
from pydantic import ValidationError

from oauthclientbridge.settings import (
    PrometheusSettings,
    TelemetryExporter,
    TelemetrySettings,
)


def test_telemetry_settings_defaults() -> None:
    settings = TelemetrySettings()
    assert settings.exporters == set()
    assert settings.endpoint == "http://localhost:4318"
    assert settings.service_name == "oauthclientbridge"
    assert settings.oauth_provider is None
    assert settings.service_instance_id is not None


def test_telemetry_settings_invalid_if_exporter_and_no_endpoint() -> None:
    with pytest.raises(ValidationError) as excinfo:
        TelemetrySettings(
            exporters={TelemetryExporter.OTLP_HTTP},
            endpoint=None,
        )
    assert "OTEL_ENDPOINT must be set if OTLP_HTTP is in TELEMETRY_EXPORTERS" in str(
        excinfo.value
    )


def test_telemetry_settings_valid_if_exporter_and_endpoint() -> None:
    settings = TelemetrySettings(
        exporters={TelemetryExporter.OTLP_HTTP},
        endpoint="http://my-collector:4318",
    )
    assert settings.exporters == {TelemetryExporter.OTLP_HTTP}
    assert settings.endpoint == "http://my-collector:4318"


def test_telemetry_settings_valid_if_no_exporter_and_no_endpoint() -> None:
    settings = TelemetrySettings(service_name="my-custom-service")
    assert settings.service_name == "my-custom-service"


def test_telemetry_settings_derives_service_instance_id() -> None:
    settings = TelemetrySettings(
        deployment_environment="preprod",
        oauth_provider="spotify",
    )
    assert settings.service_instance_id is not None
    assert settings.service_instance_id.endswith("-spotify-preprod")


def test_telemetry_settings_keeps_explicit_service_instance_id() -> None:
    settings = TelemetrySettings(
        deployment_environment="production",
        oauth_provider="soundcloud",
        service_instance_id="delta-custom",
    )
    assert settings.service_instance_id == "delta-custom"


def test_prometheus_settings_defaults() -> None:
    settings = PrometheusSettings()
    assert settings.multiproc_dir is None


def test_prometheus_settings_multiproc_dir() -> None:
    settings = PrometheusSettings(multiproc_dir=Path("/prom"))
    assert settings.multiproc_dir == Path("/prom")
