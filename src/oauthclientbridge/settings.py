import sys
from enum import StrEnum
from typing import Any

from flask import current_app
from pydantic import Field, SecretStr, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from werkzeug.local import LocalProxy


class CustomSettingsSource(EnvSettingsSource):
    def prepare_field_value(
        self,
        field_name: str,
        field: FieldInfo,
        value: Any,
        value_is_complex: bool,
    ) -> Any:
        if getattr(field.annotation, "__origin__", None) in (list, set):
            if isinstance(value, str):
                parts = [part.strip() for part in value.split(",")]
                return getattr(field.annotation, "__origin__")(
                    [part for part in parts if part]
                )

        return super().prepare_field_value(
            field_name,
            field,
            value,
            value_is_complex,
        )


class CustomBaseSettings(BaseSettings):
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ):
        return (
            init_settings,
            CustomSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )


class OAuthSettings(CustomBaseSettings):
    model_config = SettingsConfigDict(env_prefix="OAUTH_")

    client_id: str
    """Client ID provided by upstream OAuth provider."""

    client_secret: SecretStr
    """Client secret provided by upstream OAuth provider."""

    grant_type: str = "refresh_token"
    """Type of grant to request from upstream."""

    scopes: list[str] = Field(default_factory=list)
    """List of OAuth scopes to request from the upstream provider:"""

    authorization_uri: str
    """Upstream authorization URI to redirect users to."""

    token_uri: str
    """Upstream token URI."""

    refresh_uri: str | None = None
    """Upstream refresh URI. Will fallback to the token URI if not set."""

    redirect_uri: str = "http://localhost:5000/callback"
    """
    Bridge callback URI to send users back to. Should exactly match URI
    registered with the upstream provider.
    """


class FetchSettings(CustomBaseSettings):
    model_config = SettingsConfigDict(env_prefix="FETCH_")

    total_timeout: float = 20.0
    """Overall allowed timeout across all retires, backoff and retry-after time."""

    timeout: float = 5.0
    """
    Number of seconds to wait for initial connection and subsequent reads to
    upstream OAuth endpoint for a single fetch attempt.
    """

    total_retries: int = 3
    """Maximum number of retries for fetching oauth data."""

    retry_status_codes: list[int] = Field(
        default_factory=lambda: [429, 500, 502, 503, 504]
    )
    """Status codes that should be considered retryable for oauth."""

    unavailable_status_codes: list[int] = Field(
        default_factory=lambda: [429, 502, 503, 504]
    )
    """
    Status codes to treat as temporarily_unavailable when we can't decode the
    response. Remaining status codes treated as server_error.
    """

    error_types: dict[str, str] = Field(
        default_factory=lambda: {"errorTransient": "temporarily_unavailable"}
    )
    """Non-standard oauth errors and what standard errors to translate them to."""

    backoff_factor: float = 0.1
    """Backoff factor to use for not hammering the oauth server too much."""


class DatabaseSettings(CustomBaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB_")

    database: str
    """SQLite3 database to store tokens information in."""

    timeout: float = 5
    """SQlite3 database timeout to use at "connection" time."""

    pragmas: list[str] = Field(default_factory=lambda: ["PRAGMA journal_mode = WAL"])
    """SQlite3 database PRAGMAs to run at connection time for database."""


class SentrySettings(CustomBaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENTRY_")

    enabled: bool = False
    """Whether to enable Sentry."""

    dsn: SecretStr | None = None
    """Sentry DSN."""

    sample_rate: float = 1.0
    """The sample rate for error events."""

    traces_sample_rate: float = 0.0
    """The sample rate for performance monitoring traces."""

    @model_validator(mode="after")
    def check_dsn_if_enabled(self) -> "SentrySettings":
        if self.enabled and self.dsn is None:
            raise ValueError("SENTRY_DSN must be set if SENTRY_ENABLED is True")
        return self


class TelemetryExporter(StrEnum):
    OTLP_GRPC = "otlp_grpc"
    CONSOLE = "console"


class TelemetryComponent(StrEnum):
    TRACING = "tracing"
    METRICS = "metrics"


class TelemetrySettings(CustomBaseSettings):
    model_config = SettingsConfigDict(env_prefix="TELEMETRY_")

    components: set[TelemetryComponent] = Field(
        default_factory=lambda: {TelemetryComponent.TRACING},
        json_schema_extra={"env_vars_parse_as_json": False},
    )
    """Set of OpenTelemetry components to enable (e.g., TRACING, METRICS)."""

    exporters: set[TelemetryExporter] = Field(
        default_factory=set,
        json_schema_extra={"env_vars_parse_as_json": False},
    )
    """Set of OpenTelemetry exporters to use (e.g., OTLP_GRPC, CONSOLE)."""

    endpoint: str | None = "http://localhost:4317"
    """OpenTelemetry collector endpoint."""

    service_name: str = "oauthclientbridge"
    """Service name for OpenTelemetry traces and metrics."""

    metric_export_interval_seconds: float = 60.0
    """Interval in seconds for exporting metrics."""

    @model_validator(mode="after")
    def check_endpoint_if_otlp_grpc(self) -> "TelemetrySettings":
        if TelemetryExporter.OTLP_GRPC in self.exporters and self.endpoint is None:
            raise ValueError(
                "OTEL_ENDPOINT must be set if OTLP_GRPC is in TELEMETRY_EXPORTERS"
            )
        return self


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class LogSettings(CustomBaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOG_")

    level: LogLevel = LogLevel.INFO
    """Default log level to use for all events."""

    json_output: bool = Field(default_factory=lambda: not sys.stdout.isatty())
    """Whether to output logs in JSON format."""

    colors: bool = Field(default_factory=sys.stdout.isatty)
    """Whether to use colors in console output."""

    access_log_format: str = "{message}"
    """Format string to use for access logs."""


class Settings(CustomBaseSettings):
    """
    Application settings for oauthclientbridge.
    """

    model_config = SettingsConfigDict(env_prefix="BRIDGE_")

    auth_realm: str = "oauthclientbridge"
    """Realm to present for basic auth."""

    callback_template: str = """{% if error %}
  {{ error }}{% if description %}: {{ description }}{% endif %}
{% else %}
  <form action="token" method="POST">
    Client ID: <input name="client_id" value="{{ client_id }}" />
    Client Secret: <input name="client_secret" value="{{ client_secret }}" />
    Grant type: <input name="grant_type" value="client_credentials" />
    <button>Fetch token</button>
  </form>
  <form action="revoke" method="POST">
    Client ID: <input name="client_id" value="{{ client_id }}" />
    <button>Revoke token</button>
  </form>
{% endif %}
"""
    """
    Jinja2 template to use for the callback page. Possible context values are:
    error, description, client_id, client_secret. Should be setup to give the
    client_id and client_secret to the user. Either directly or passing the
    value back to the parent window if this is being run in a pop-up window.
    """

    num_proxies: int = 0
    """Number proxies to expect in front of us. Used for handling X-Forwarded-For"""

    error_levels: dict[str, str] = Field(
        default_factory=lambda: {
            "access_denied": "INFO",
            "invalid_state": "WARNING",
            "invalid_request": "WARNING",
            "temporarily_unavailable": "INFO",
        }
    )
    """Log levels to use for errors in callback flow."""

    oauth: OAuthSettings = Field(default_factory=lambda: OAuthSettings())  # pyright: ignore[reportCallIssue]
    fetch: FetchSettings = Field(default_factory=lambda: FetchSettings())  # pyright: ignore[reportCallIssue]
    database: DatabaseSettings = Field(default_factory=lambda: DatabaseSettings())  # pyright: ignore[reportCallIssue]
    sentry: SentrySettings = Field(default_factory=lambda: SentrySettings())  # pyright: ignore[reportCallIssue]
    log: LogSettings = Field(default_factory=lambda: LogSettings())  # pyright: ignore[reportCallIssue]
    otel: TelemetrySettings = Field(default_factory=lambda: TelemetrySettings())  # pyright: ignore[reportCallIssue]


current_settings: LocalProxy[Settings] = LocalProxy(
    lambda: current_app.config["SETTINGS"]
)
