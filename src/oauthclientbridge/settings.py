from enum import StrEnum, auto

from flask import current_app
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from werkzeug.local import LocalProxy


class OAuthSettings(BaseSettings):
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


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB_")

    database: str
    """SQLite3 database to store tokens information in."""

    timeout: float = 5
    """SQlite3 database timeout to use at "connection" time."""

    pragmas: list[str] = Field(default_factory=lambda: ["PRAGMA journal_mode = WAL"])
    """SQlite3 database PRAGMAs to run at connection time for database."""


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


class OtelExporterProtocol(StrEnum):
    OTLP_GRPC = auto()
    CONSOLE = auto()


class TelemetrySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TELEMETRY_")

    # TODO: Consider having a sub-object per exporter type instead?
    # This could also allow for passing in an exporter instance?
    exporter: OtelExporterProtocol | None = None
    """OpenTelemetry exporter (e.g., OTLP_GRPC, CONSOLE)."""

    endpoint: str | None = "http://localhost:4317"
    """OpenTelemetry collector endpoint."""

    service_name: str = "oauthclientbridge"
    """Service name for OpenTelemetry traces and metrics."""

    @model_validator(mode="after")
    def check_endpoint_if_otlp_grpc(self) -> "TelemetrySettings":
        if self.exporter == OtelExporterProtocol.OTLP_GRPC and self.endpoint is None:
            raise ValueError(
                "OTEL_ENDPOINT must be set if OTEL_EXPORTER_PROTOCOL is OTLP_GRPC"
            )
        return self


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
    otel: TelemetrySettings = Field(default_factory=lambda: TelemetrySettings())  # pyright: ignore[reportCallIssue]


current_settings: LocalProxy[Settings] = LocalProxy(
    lambda: current_app.config["SETTINGS"]
)
