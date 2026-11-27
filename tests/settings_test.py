from http import HTTPStatus
from pathlib import Path

import pytest
from pydantic import ValidationError

from oauthclientbridge.settings import (
    ClientResetError,
    FetchSettings,
    FetchTransport,
    PrometheusSettings,
    Settings,
    TelemetryExporter,
    TelemetrySettings,
)
from oauthclientbridge.types import UpstreamGrantType


def test_telemetry_settings_defaults() -> None:
    settings = TelemetrySettings()
    assert settings.exporters == set()
    assert settings.endpoint == "http://localhost:4318"
    assert settings.service_name == "oauthclientbridge"
    assert settings.service_namespace == "oauthclientbridge"
    assert settings.oauth_provider is None
    assert settings.service_instance_id is None
    assert settings.metric_export_interval_seconds == 60.0


def test_telemetry_settings_invalid_if_exporter_and_no_endpoint() -> None:
    with pytest.raises(ValidationError) as excinfo:
        TelemetrySettings(
            exporters={TelemetryExporter.OTLP_HTTP},
            endpoint=None,
        )
    assert (
        "TELEMETRY_ENDPOINT must be set if OTLP_HTTP is in TELEMETRY_EXPORTERS"
        in str(excinfo.value)
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


def test_telemetry_settings_defers_default_service_instance_id() -> None:
    settings = TelemetrySettings(
        deployment_environment="preprod",
        oauth_provider="spotify",
    )

    assert settings.service_instance_id is None


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


def test_fetch_settings_reset_errors_default_to_unhealthy_paths() -> None:
    settings = FetchSettings()

    assert settings.client_reset_errors == (
        ClientResetError.HTTP_502,
        ClientResetError.HTTP_503,
        ClientResetError.HTTP_504,
        ClientResetError.CONNECTION_ERROR,
        ClientResetError.CONNECTION_TIMEOUT,
        ClientResetError.TLS_ERROR,
    )


def test_fetch_settings_rejects_http_reset_error_that_is_not_retryable() -> None:
    with pytest.raises(ValidationError, match="client reset HTTP errors"):
        FetchSettings(client_reset_errors=(ClientResetError.HTTP_500,))


def test_fetch_settings_permits_configured_http_reset_error() -> None:
    settings = FetchSettings(
        retry_status_codes=(HTTPStatus.INTERNAL_SERVER_ERROR,),
        client_reset_errors=(ClientResetError.HTTP_500,),
    )

    assert settings.client_reset_errors == (ClientResetError.HTTP_500,)


def test_fetch_settings_accepts_rfc_grant_types_for_read_timeout_retries() -> None:
    settings = FetchSettings(
        read_timeout_retry_grant_types=("authorization_code", "refresh_token")
    )

    assert settings.read_timeout_retry_grant_types == (
        UpstreamGrantType.AUTHORIZATION_CODE,
        UpstreamGrantType.REFRESH_TOKEN,
    )


def test_prepared_settings_load_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FETCH_TRANSPORT", "prepared")
    monkeypatch.setenv("FETCH_PREPARED_VERSIONS", '["HTTP/1.1","HTTP/2"]')
    monkeypatch.setenv("FETCH_PREPARED_MAX_STREAMS", "7")
    configured = FetchSettings()
    assert configured.transport == FetchTransport.PREPARED
    assert configured.prepared_versions == ("HTTP/1.1", "HTTP/2")
    assert configured.prepared_max_streams == 7


@pytest.mark.parametrize(
    "policy",
    [
        {"transport": "unknown"},
        {"prepared_versions": []},
        {"prepared_versions": ["HTTP/2", "HTTP/2"]},
        {"prepared_versions": ["HTTP/3"]},
        {"prepared_max_streams": 0},
        {
            "transport": "prepared",
            "read_timeout_retry_grant_types": ["authorization_code"],
        },
    ],
)
def test_prepared_settings_reject_invalid_policy(policy: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FetchSettings.model_validate(policy)


@pytest.mark.parametrize(
    "uri",
    [
        "http://provider.example.com/token",
        "https://user:secret@provider.example.com/token",
        "https:///token",
        "https://provider.example.com:invalid/token",
    ],
)
def test_prepared_settings_require_valid_https_token_uri(
    settings: Settings, uri: str
) -> None:
    values = settings.model_dump()
    values["fetch"]["transport"] = "prepared"
    values["oauth"]["token_uri"] = uri
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_prepared_settings_pin_token_and_refresh_to_same_origin(
    settings: Settings,
) -> None:
    values = settings.model_dump()
    values["fetch"]["transport"] = "prepared"
    values["oauth"]["refresh_uri"] = "https://other.example.com/refresh"
    with pytest.raises(ValidationError, match="one token/refresh origin"):
        Settings.model_validate(values)
    values["oauth"]["refresh_uri"] = (
        "https://PROVIDER.example.com:443/refresh?purpose=refresh"
    )
    configured = Settings.model_validate(values)
    assert configured.oauth.prepared_origin() == "https://provider.example.com/"
