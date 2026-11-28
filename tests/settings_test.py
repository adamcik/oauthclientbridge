from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from oauthclientbridge.settings import (
    FetchSettings,
    PrometheusSettings,
    Settings,
    TelemetryExporter,
    TelemetrySettings,
)


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


def test_prepared_settings_load_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FETCH_VERSIONS", '["HTTP/1.1","HTTP/2"]')
    monkeypatch.setenv("FETCH_MAX_STREAMS", "7")
    monkeypatch.setenv("FETCH_MAX_ADMISSION_AGE", "3600")
    monkeypatch.setenv("FETCH_PREPARE_AHEAD", "60")
    configured = FetchSettings()
    assert configured.versions == ("HTTP/1.1", "HTTP/2")
    assert configured.max_streams == 7
    assert configured.max_admission_age == 3600
    assert configured.prepare_ahead == 60


@dataclass(frozen=True)
class PreparationWindowCase:
    id: str
    max_age: float
    ahead: float


@pytest.mark.parametrize(
    "case",
    [
        PreparationWindowCase(id="zero-age", max_age=0, ahead=1),
        PreparationWindowCase(id="nonfinite-age", max_age=float("nan"), ahead=1),
        PreparationWindowCase(id="zero-headroom", max_age=60, ahead=0),
        PreparationWindowCase(id="nonfinite-headroom", max_age=60, ahead=float("inf")),
        PreparationWindowCase(id="headroom-at-cap", max_age=60, ahead=60),
        PreparationWindowCase(id="headroom-beyond-cap", max_age=60, ahead=61),
    ],
    ids=lambda case: case.id,
)
def test_preparation_window_requires_positive_headroom_before_age_cap(
    case: PreparationWindowCase,
) -> None:
    with pytest.raises(ValidationError):
        FetchSettings(max_admission_age=case.max_age, prepare_ahead=case.ahead)


@pytest.mark.parametrize(
    "policy",
    [
        {"transport": "unknown"},
        {"versions": []},
        {"versions": ["HTTP/2", "HTTP/2"]},
        {"versions": ["HTTP/3"]},
        {"max_streams": 0},
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
    values["oauth"]["token_uri"] = uri
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_prepared_settings_pin_token_and_refresh_to_same_origin(
    settings: Settings,
) -> None:
    values = settings.model_dump()
    values["oauth"]["refresh_uri"] = "https://other.example.com/refresh"
    with pytest.raises(ValidationError, match="one token/refresh origin"):
        Settings.model_validate(values)
    values["oauth"]["refresh_uri"] = (
        "https://PROVIDER.example.com:443/refresh?purpose=refresh"
    )
    configured = Settings.model_validate(values)
    assert configured.oauth.upstream_origin() == "https://provider.example.com/"
