from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings for oauthclientbridge.

    Settings are loaded from environment variables with the prefix OAUTH_.
    For example, OAUTH_SECRET_KEY will map to the secret_key field.
    """

    model_config = SettingsConfigDict(
        env_prefix="BRIDGE_",
    )

    # TODO: Add back proper oauth prefix, or split into sub-models for DB etc.
    # TODO: After splitting load from toml file?

    secret_key: SecretStr = Field(
        description=(
            "Secret key used for encrypting session cookies used in initial OAuth flow,"
            " MUST be set."
        ),
    )
    database: str = Field(
        description="SQLite3 database to store tokens information in, MUST be set.",
    )
    database_timeout: float = Field(
        5, description='SQlite3 database timeout to use at "connection" time.'
    )
    database_pragmas: list[str] = Field(
        ["PRAGMA journal_mode = WAL"],
        description="SQlite3 database PRAGMAs to run at connection time for database.",
    )
    client_id: str = Field(
        description="Client ID provided by upstream OAuth provider, MUST be set."
    )
    client_secret: SecretStr = Field(
        description="Client secret provided by upstream OAuth provider, MUST be set.",
    )
    grant_type: str = Field(
        "refresh_token", description="Type of grant to request from upstream."
    )
    scopes: list[str] = Field(
        [], description="List of OAuth scopes to request from the upstream provider:"
    )
    authorization_uri: str = Field(
        description="Upstream authorization URI to redirect users to, MUST be set.",
    )
    token_uri: str = Field(
        description="Upstream token URI. MUST be set.",
    )
    refresh_uri: str | None = Field(
        None,
        description="Upstream refresh URI. Will fallback to the token URI if not set.",
    )
    fetch_total_timeout: float = Field(
        20.0,
        description="Overall allowed timeout across all retires, backoff and retry-after time.",
    )
    fetch_timeout: float = Field(
        5.0,
        description=(
            "Number of seconds to wait for initial connection and subsequent reads to"
            " upstream OAuth endpoint for a single fetch attempt."
        ),
    )
    fetch_total_retries: int = Field(
        3, description="Maximum number of retries for fetching oauth data."
    )
    fetch_retry_status_codes: list[int] = Field(
        [429, 500, 502, 503, 504],
        description="Status codes that should be considered retryable for oauth.",
    )
    fetch_unavailable_status_codes: list[int] = Field(
        [429, 502, 503, 504],
        description=(
            "Status codes to treat as temporarily_unavailable when we can't decode the"
            " response. Remaining status codes treated as server_error."
        ),
    )
    fetch_error_types: dict[str, str] = Field(
        {"errorTransient": "temporarily_unavailable"},
        description="Non-standard oauth errors and what standard errors to translate them to.",
    )
    fetch_backoff_factor: float = Field(
        0.1,
        description="Backoff factor to use for not hammering the oauth server too much.",
    )
    redirect_uri: str = Field(
        "http://localhost:5000/callback",
        description=(
            "Bridge callback URI to send users back to. Should exactly match URI"
            " registered with the upstream provider."
        ),
    )
    realm: str = Field(
        "oauthclientbridge", description="Realm to present for basic auth."
    )
    callback_template: str = Field(
        """{% if error %}
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
""",
        description=(
            "Jinja2 template to use for the callback page. Possible context values are:"
            " error, description, client_id, client_secret. Should be setup to give the"
            " client_id and client_secret to the user. Either directly or passing the"
            " value back to the parent window if this is being run in a pop-up window."
        ),
    )
    num_proxies: int = Field(
        0,
        description=(
            "Number proxies to expect in front of us. Used for handling X-Forwarded-For"
        ),
    )
    log_file: str | None = Field(
        None,
        description="Additional log file for application level logging, set to None to disable.",
    )
    log_file_level: str = Field("INFO", description="Log level for file logging.")
    log_file_format: str = Field(
        "%(asctime)s %(levelname)s: %(message)s " "[in %(pathname)s:%(lineno)d]",
        description="Log format for file logging.",
    )
    log_file_max_bytes: int = Field(
        0, description="Max bytes to pass to the RotatingFileHandler logging handler."
    )
    log_file_backup_count: int = Field(
        0, description="Number of backups that the RotatingFileHandler should keep."
    )
    log_email: list[str] = Field(
        [],
        description="List of addresses to send logging emails to, leave empty to disable.",
    )
    log_email_level: str = Field("ERROR", description="Log level for email logging.")
    log_email_format: str = Field(
        """%(message)s

Remote address:   %(request_remote_address)s
Time:             %(asctime)s
Message type:     %(levelname)s
Path:             %(request_path)s
Location:         %(pathname)s:%(lineno)d
Module:           %(module)s
Function:         %(funcName)s
""",
        description="Log format for email logging.",
    )
    log_email_host: str = Field(
        "localhost", description="SMTP host to use for email logging."
    )
    log_email_from: str = Field(
        "oauthclientbridge@localhost",
        description="From address to user for email logging.",
    )
    log_email_subject: str = Field(
        "oauthclientbridge: %(request_base_url)s",
        description="Subject line to use for email logging.",
    )
    error_log_levels: dict[str, str] = Field(
        {
            "access_denied": "INFO",
            "invalid_state": "WARNING",
            "invalid_request": "WARNING",
            "temporarily_unavailable": "INFO",
        },
        description="Log levels to use for errors in callback flow.",
    )
