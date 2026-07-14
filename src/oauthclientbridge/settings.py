import logging
import socket
import sys
from enum import IntEnum, StrEnum
from http import HTTPStatus
from importlib.metadata import PackageNotFoundError, metadata, version
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
    """Default OAuth scopes to request from the upstream provider."""

    allowed_scopes: set[str] | None = None
    """Allowed requested OAuth scopes. None permits dynamic requested scopes."""

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

    retry_budget_capacity: int = 8
    """Per-process maximum number of outgoing retries held in budget."""

    retry_budget_refill_per_initial: float = 0.25
    """How much retry budget each initial outgoing request replenishes."""

    retry_status_codes: tuple[HTTPStatus, ...] = Field(
        (
            HTTPStatus.TOO_MANY_REQUESTS,
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

    backoff_jitter_min: float = 0.75
    """Lower multiplier bound for retry backoff jitter.

    Values below 1.0 allow some retries to happen sooner than the nominal
    backoff delay so jitter spreads clients around that base delay instead of
    only delaying them longer. `Retry-After` still acts as a floor when the
    provider supplies one.
    """

    backoff_jitter_max: float = 1.25
    """Upper multiplier bound for retry backoff jitter around the base delay."""


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

    traces_sample_rate: float = 1.0
    """The sample rate for performance monitoring traces."""

    traces_sample_rate_overrides: dict[str, float] = Field(default_factory=dict)
    """Per-path performance trace sample-rate overrides."""

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

    service_namespace: str = "oauthclientbridge"
    """Service namespace for OpenTelemetry resource attributes."""

    service_version: str = Field(
        default_factory=lambda: _current_package_version(),
    )
    """Service version for OpenTelemetry resources and Prometheus build info."""

    deployment_environment: str = "unknown"
    """Deployment environment for telemetry data (e.g., production, staging)."""

    oauth_provider: str | None = None
    """OAuth provider identifier for telemetry resource attributes."""

    service_instance_id: str | None = None
    """Stable service instance identifier for telemetry resource attributes."""

    vcs_revision: str | None = None
    """VCS revision (e.g., git commit SHA) for telemetry data."""

    @model_validator(mode="after")
    def default_vcs_revision_from_package_metadata(self) -> "TelemetrySettings":
        if self.vcs_revision is None:
            self.vcs_revision = _current_package_revision()
        return self

    @model_validator(mode="after")
    def default_service_instance_id(self) -> "TelemetrySettings":
        if self.service_instance_id is not None:
            return self

        parts = [socket.gethostname()]
        if self.oauth_provider:
            parts.append(self.oauth_provider)
        parts.append(self.deployment_environment)
        self.service_instance_id = "-".join(parts)
        return self

    metric_export_interval_seconds: float = 60.0
    """Interval in seconds for exporting metrics."""

    @model_validator(mode="after")
    def check_endpoint_if_otlp_http(self) -> "TelemetrySettings":
        if TelemetryExporter.OTLP_HTTP in self.exporters and self.endpoint is None:
            raise ValueError(
                "TELEMETRY_ENDPOINT must be set if OTLP_HTTP is in TELEMETRY_EXPORTERS"
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

    access_log_format: str = '{client.address} "{http.request.method} {url.path} {network.protocol.version}" {http.response.status_code} {http.response.body.size} "{user_agent.original}"'
    """Format string to use for access logs."""


class Settings(BaseSettings):
    """
    Application settings for oauthclientbridge.
    """

    model_config = SettingsConfigDict(env_prefix="BRIDGE_")

    auth_realm: str = "oauthclientbridge"
    """Realm to present for basic auth."""

    metrics_enabled: bool = False
    """Whether to expose the Prometheus metrics endpoint."""

    metrics_token: SecretStr | None = None
    """Optional bearer token required to access the metrics endpoint."""

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

    callback_template_file: Path | None = None
    """Optional path to file containing callback_template."""

    callback_content_security_policy: str | None = (
        "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    """CSP for callback HTML. Set to None only when a custom template requires it."""

    error_levels: dict[str, LogLevel] = Field(
        default_factory=lambda: {
            "access_denied": LogLevel.INFO,
            "invalid_state": LogLevel.WARNING,
            "invalid_request": LogLevel.WARNING,
            "temporarily_unavailable": LogLevel.INFO,
        }
    )
    """Log levels to use for errors in callback flow."""

    revoked_grant_workaround_user_agents: str | None = None
    """
    User-Agent matcher controlling the revoked-grant workaround. When a request
    to /token is for a client whose stored token is already locally revoked,
    and the request User-Agent matches this regular expression, the bridge
    returns a synthetic bearer token instead of 400 invalid_grant. This
    deliberately provokes an upstream 401 from the provider API so affected
    clients stop retrying refresh failures against the bridge. Empty or unset
    disables the workaround.
    """

    revoked_grant_workaround_access_token: str = (
        "OAUTHCLIENTBRIDGE_REVOKED_GRANT_WORKAROUND"
    )
    """
    Synthetic access token returned by the revoked-grant workaround. This must
    remain an obvious sentinel value and must not be a real provider token.
    """

    revoked_grant_workaround_expires_in: int = 300
    """
    Expiry in seconds for synthetic access tokens returned by the revoked-grant
    workaround. It should be long enough for affected clients to make a
    follow-up API request, observe an upstream 401, and enter their local
    backoff path instead of hammering the bridge.
    """

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


def _current_package_version() -> str:
    try:
        return version("oauthclientbridge")
    except PackageNotFoundError:
        return "unknown"


def _current_package_revision() -> str | None:
    try:
        pkg_metadata = metadata("oauthclientbridge")
    except PackageNotFoundError:
        return None

    revision = pkg_metadata.get("Vcs-Revision")
    if revision:
        return revision

    return None
