# OAuth-Client-Bridge

The OAuth2 Client Bridge provides a service to "convert" Authorization
Code Grants to Clients Grants suitable for use in "native applications"
where it is impractical to use Authorization Code grants directly.

## Background

This code exists to solve the "problem" of trying to authenticate native
applications against OAuth providers that only allow for long term
grants via Authorization Code Grants without giving the secrets to the
native application.

After quite some back an forth this solution was devised as it felt the
least hackish, and somewhat cleanly combines two existing flows instead
of creating a strange hybrid flow.

## Installation

Install by running:

    pip install OAuth-Client-Bridge

## Settings

Settings are managed by Pydantic and loaded from environment variables. Each
setting is prefixed based on its category (e.g., `OAUTH_`, `DB_`, `BRIDGE_`,
`FETCH_`). Refer to the `Settings` class in `src/oauthclientbridge/settings.py`
for all available options and their prefixes. Flask-specific settings (e.g.,
`SECRET_KEY`, `SESSION_COOKIE_SECURE`) are loaded directly by Flask from
environment variables prefixed with `FLASK_`. A minimal setup should define the
following environment variables (see `.env.example`):

-   `FLASK_SECRET_KEY`: Secret key used for encrypting session cookies.
-   `DB_DATABASE`: SQLite3 database path.
-   `OAUTH_CLIENT_ID`: Client ID from your OAuth provider.
-   `OAUTH_CLIENT_SECRET`: Client secret from your OAuth provider.
-   `OAUTH_AUTHORIZATION_URI`: Upstream authorization URI.
-   `OAUTH_TOKEN_URI`: Upstream token URI.

Once these are set (e.g., by sourcing a `.env` file), you can initialize the
database:

    FLASK_APP=oauthclientbridge flask initdb

Run the development server:

    FLASK_APP=oauthclientbridge flask run

`flask run` uses the app factory directly. This intentionally does not start
runtime background services. In practice that means request handling works the
same, but background-refreshed metrics such as `oauth_token_records` are only
kept fresh in the production WSGI entrypoint, which calls
`start_runtime_services(app)` after the database has been initialized.

Additionally you might want to run `cleandb` as a cron job to clear out
stale data every now and then.:

    FLASK_APP=oauthclientbridge flask cleandb

## Setting up a production instance

-   Always use HTTPS since we are passing access tokens around.
-   Set `FLASK_SESSION_COOKIE_SECURE` to `True` to ensure cookies are only sent
    over HTTPS.
-   Ideally also set `FLASK_SESSION_COOKIE_DOMAIN` and `FLASK_SESSION_COOKIE_PATH`.
-   Ensure the deployment passes the correct request scheme, host, and client
    address to the application. The bridge does not reinterpret forwarded
    headers. Deployments that do not provide these values directly may need to
    wrap `app.wsgi_app` with a topology-appropriate
    `werkzeug.middleware.proxy_fix.ProxyFix` in their WSGI entrypoint.
-   `OAUTH_SCOPES` is used when the authorization request omits `scope`.
    Set `OAUTH_ALLOWED_SCOPES` to restrict requested scopes to that allowlist.
    Leaving it unset permits dynamic scopes for compatibility and should only be
    used when the caller is trusted.
-   Callback HTML has a restrictive default CSP plus no-referrer, nosniff, and
    permissions-policy headers. Custom callback templates using scripts or
    external resources must set `BRIDGE_CALLBACK_CONTENT_SECURITY_POLICY` to a
    suitable policy, or explicitly disable it with an empty
    `BRIDGE_CALLBACK_CONTENT_SECURITY_POLICY` value.
-   `/metrics` is disabled by default. Set `BRIDGE_METRICS_ENABLED=True` and
    `BRIDGE_METRICS_TOKEN` to require bearer authentication. Metrics expose
    operational information; additionally restrict the route to internal
    networks at Caddy or another edge proxy.

For further details on deploying Flask applications see the [upstream
documentation][].

The following code snippet can be used to create a popup pointed at the
oauth server, and the poll the it for the results:

    var target = 'https://example.net/oauth';
    var targetOrigin = 'https://example.net'

    window.addEventListener('message', function(event) {
      if (event.origin !== targetOrigin) return;

      if (event.data['error']) {
        // Update webpage with error data.
      } else {
        // Update webpage with client_id and client_secret.
      }

      event.source.close();
    }, false);

    var child = window.open(target);
    var interval = setInterval(function() {
      if (child.closed) {
        clearInterval(interval);
      } else {
        child.postMessage('oauthclientbridge', targetOrigin)
      }
    }, 1000);

To get the snippet above to work setup the bridge with the following
template which will listen for the `postMessage` and then respond with
the results.:

        BRIDGE_CALLBACK_TEMPLATE

[upstream documentation]: http://flask.pocoo.org/docs/latest/deploying/

## OpenTelemetry Integration

This project integrates with OpenTelemetry for distributed tracing and metrics
collection. To enable OpenTelemetry, you need to configure the following
environment variables:

-   `TELEMETRY_COMPONENTS`: A comma-separated list of OpenTelemetry components
    to enable. Valid values are `tracing` and `metrics`. For example:
    `TELEMETRY_COMPONENTS=tracing,metrics`.

-   `TELEMETRY_EXPORTERS`: A comma-separated list of exporters to use for traces
    and metrics. Valid values are `otlp_http` and `console`. For example:
    `TELEMETRY_EXPORTERS=otlp_http`.

-   `TELEMETRY_ENDPOINT`: The OTLP collector endpoint (e.g.,
    `http://localhost:4318`). Required if `otlp_http` exporter is used.

-   `TELEMETRY_SERVICE_NAME`: The name of the service to be reported to
    OpenTelemetry (defaults to `oauthclientbridge`).

-   `TELEMETRY_SERVICE_VERSION`: Service version reported as
    `service.version` in OpenTelemetry resources (defaults to package version).

-   `TELEMETRY_DEPLOYMENT_ENVIRONMENT`: Deployment environment reported as
    `deployment.environment` in OpenTelemetry resources (defaults to
    `unknown`).

-   `TELEMETRY_VCS_REVISION`: VCS revision (for example, git commit SHA)
    reported as `vcs.revision` in OpenTelemetry resources.

-   `TELEMETRY_METRIC_EXPORT_INTERVAL_SECONDS`: The interval in seconds at which
    metrics are exported (defaults to `60`).

Metrics are pushed over OTLP HTTP as this is the only exporter we support.

## Retry And Refresh Token Semantics

The bridge applies a provider-facing retry policy to upstream token endpoint
responses and a narrower local invalidation policy to stored refresh tokens.

-   Retryable upstream failures are determined by HTTP status and transport
    outcome, not by contradictory OAuth error payloads.
-   `429`, `502`, `503`, `504`, connection failures, timeouts, and similar
    transient upstream failures are retried according to the configured fetch
    budget and deadline.
-   If those retries are exhausted, the bridge surfaces
    `temporarily_unavailable` to its caller and keeps any stored refresh token.
-   Stored refresh tokens are only invalidated on an authoritative,
    non-retryable token refresh failure. For Spotify, this means `400` with
    OAuth error `invalid_grant`.
-   A retryable response such as `503` with a contradictory body like
    `{"error":"invalid_grant"}` is treated as transient provider failure, not
    proof that the stored refresh token is dead.
-   If the provider returns a new `refresh_token` on success, the bridge stores
    it. If success omits `refresh_token`, the bridge keeps the existing stored
    refresh token.

This behavior matches Spotify's documented refresh-token expiration semantics:
expired refresh tokens return `400 Bad Request` with `invalid_grant` and must
be discarded, while transient token-endpoint failures should not cause local
token invalidation.

For selected broken clients, the bridge can also serve a synthetic bearer token
after a refresh token has already been locally revoked. This is a temporary
workaround for clients that do not back off on token refresh `invalid_grant`
responses but do stop retrying after a `401` from the provider Web API. By
provoking that upstream `401`, the bridge avoids repeated refresh attempts from
those clients against a grant it already knows is dead.
