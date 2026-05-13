import logging
import sys
from enum import IntEnum, StrEnum
from http import HTTPStatus
from pathlib import Path

from flask import current_app
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)
from werkzeug.local import LocalProxy

from oauthclientbridge.errors import OAuthError


class OAuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OAUTH_")

    client_id: str
    """Client ID provided by upstream OAuth provider."""

    client_secret: SecretStr
    """Client secret provided by upstream OAuth provider."""

    grant_type: str = "refresh_token"
    """Type of grant to request from upstream."""

    scopes: set[str] = Field(default_factory=set)
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


class FetchSettings(BaseSettings):
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

    retry_status_codes: tuple[HTTPStatus, ...] = Field(
        (
            HTTPStatus.TOO_MANY_REQUESTS,
            HTTPStatus.INTERNAL_SERVER_ERROR,
            HTTPStatus.BAD_GATEWAY,
            HTTPStatus.SERVICE_UNAVAILABLE,
            HTTPStatus.GATEWAY_TIMEOUT,
        ),
    )
    """Status codes that should be considered retryable for oauth."""

    unavailable_status_codes: tuple[HTTPStatus, ...] = Field(
        (
            HTTPStatus.TOO_MANY_REQUESTS,
            HTTPStatus.BAD_GATEWAY,
            HTTPStatus.SERVICE_UNAVAILABLE,
            HTTPStatus.GATEWAY_TIMEOUT,
        )
    )
    """
    Status codes to treat as temporarily_unavailable when we can't decode the
    response. Remaining status codes treated as server_error.
    """

    error_types: dict[str, OAuthError] = Field(
        default_factory=lambda: {"errorTransient": OAuthError.TEMPORARILY_UNAVAILABLE},
    )
    """Non-standard oauth errors and what standard errors to translate them to."""

    backoff_factor: float = 0.1
    """Backoff factor to use for not hammering the oauth server too much."""


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB_")

    database: str = "./sqlite.db"
    """SQLite3 database to store tokens information in."""

    timeout: float = 5
    """SQlite3 database timeout to use at "connection" time."""

    pragmas: list[str] = Field(
        default_factory=lambda: ["PRAGMA journal_mode = WAL"],
    )
    """ SQlite3 database PRAGMAs to run at connection time for database.
    Note, this is JSON formatted in the ENV."""


class SentrySettings(BaseSettings):
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
    OTLP_HTTP = "otlp_http"
    CONSOLE = "console"


class TelemetryComponent(StrEnum):
    TRACING = "tracing"
    METRICS = "metrics"


class TelemetrySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TELEMETRY_")

    components: set[TelemetryComponent] = Field(
        default_factory=lambda: {TelemetryComponent.TRACING}
    )
    """Set of OpenTelemetry components to enable (e.g., TRACING, METRICS)."""

    exporters: set[TelemetryExporter] = Field(
        default_factory=set,
    )
    """Set of OpenTelemetry exporters to use (e.g., OTLP_HTTP, CONSOLE)."""

    endpoint: str | None = "http://localhost:4318"
    """OpenTelemetry collector endpoint."""

    service_name: str = "oauthclientbridge"
    """Service name for OpenTelemetry traces and metrics."""

    metric_export_interval_seconds: float = 60.0
    """Interval in seconds for exporting metrics."""

    @model_validator(mode="after")
    def check_endpoint_if_otlp_http(self) -> "TelemetrySettings":
        if TelemetryExporter.OTLP_HTTP in self.exporters and self.endpoint is None:
            raise ValueError(
                "OTEL_ENDPOINT must be set if OTLP_HTTP is in TELEMETRY_EXPORTERS"
            )
        return self


class PrometheusSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROMETHEUS_")

    multiproc_dir: Path | None = None
    """Directory for prometheus-client multiprocess mode files."""


class LogLevel(IntEnum):
    CRITICAL = logging.CRITICAL
    ERROR = logging.ERROR
    WARNING = logging.WARNING
    INFO = logging.INFO
    DEBUG = logging.DEBUG


class LogSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOG_")

    level: LogLevel = LogLevel.INFO
    """Default log level to use for all events."""

    json_output: bool = Field(default_factory=lambda: not sys.stdout.isatty())
    """Whether to output logs in JSON format."""

    colors: bool = Field(default_factory=sys.stdout.isatty)
    """Whether to use colors in console output."""

    access_log_format: str = '{client.address} "{http.request.method} {url.path} {network.protocol.version}" {http.response.status_code} {http.response.body.size} "{http.request.header.referer}" "{user_agent.original}"'
    """Format string to use for access logs."""


class Settings(BaseSettings):
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

    callback_template_file: Path | None = None
    """Optional path to file containing callback_template."""

    error_levels: dict[str, LogLevel] = Field(
        default_factory=lambda: {
            "access_denied": LogLevel.INFO,
            "invalid_state": LogLevel.WARNING,
            "invalid_request": LogLevel.WARNING,
            "temporarily_unavailable": LogLevel.INFO,
        }
    )
    """Log levels to use for errors in callback flow."""

    oauth: OAuthSettings = Field(default_factory=lambda: OAuthSettings())  # pyright: ignore[reportCallIssue]
    fetch: FetchSettings = Field(default_factory=lambda: FetchSettings())  # pyright: ignore[reportCallIssue]
    database: DatabaseSettings = Field(default_factory=lambda: DatabaseSettings())  # pyright: ignore[reportCallIssue]
    sentry: SentrySettings = Field(default_factory=lambda: SentrySettings())  # pyright: ignore[reportCallIssue]
    log: LogSettings = Field(default_factory=lambda: LogSettings())  # pyright: ignore[reportCallIssue]
    otel: TelemetrySettings = Field(default_factory=lambda: TelemetrySettings())  # pyright: ignore[reportCallIssue]
    prometheus: PrometheusSettings = Field(default_factory=lambda: PrometheusSettings())  # pyright: ignore[reportCallIssue]

    @model_validator(mode="after")
    def load_callback_template_file(self) -> "Settings":
        if self.callback_template_file:
            self.callback_template = self.callback_template_file.read_text()
        return self


current_settings: LocalProxy[Settings] = LocalProxy(
    lambda: current_app.config["SETTINGS"]
)
