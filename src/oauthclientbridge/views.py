import hmac
import re
from http import HTTPStatus
from typing import Any

import flask
import structlog
from flask import Blueprint
from opentelemetry import trace
from opentelemetry.semconv.attributes.exception_attributes import (
    EXCEPTION_MESSAGE,
    EXCEPTION_TYPE,
)

from oauthclientbridge import client, crypto, db, oauth, stats, telemetry
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import LogLevel, current_settings

logger: structlog.BoundLogger = structlog.get_logger()

routes = Blueprint("views", __name__)


def _updated_fields(
    original: dict[str, Any], modified: dict[str, Any]
) -> tuple[str, ...]:
    return tuple(
        sorted(
            key
            for key in set(original).union(modified)
            if original.get(key) != modified.get(key)
        )
    )


@routes.route("/")
def authorize() -> flask.Response:
    """Store random state in session cookie and redirect to auth endpoint."""
    redirect_uri: str | None = flask.request.args.get("redirect_uri")
    if redirect_uri and redirect_uri != current_settings.oauth.redirect_uri:
        return _error(OAuthError.INVALID_REQUEST, "Wrong redirect_uri.")

    default_scope = " ".join(current_settings.oauth.scopes)
    scope = flask.request.args.get("scope", default_scope)
    allowed_scopes = current_settings.oauth.allowed_scopes
    if allowed_scopes is not None and not set(scope.split()).issubset(allowed_scopes):
        return _error(OAuthError.INVALID_SCOPE, "Requested scope is not allowed.")
    state = crypto.generate_key()

    flask.session["client_state"] = flask.request.args.get("state")
    flask.session["state"] = state

    return oauth.redirect(
        current_settings.oauth.authorization_uri,
        client_id=current_settings.oauth.client_id,
        response_type="code",
        redirect_uri=current_settings.oauth.redirect_uri,
        scope=scope,
        state=state,
    )


@routes.route("/callback")
def callback() -> flask.Response:
    """Validate callback and trade in code for a token."""

    error: str | None = None
    desc: str | None = None
    client_state: str | None = flask.session.pop("client_state", None)
    state: str | None = flask.session.pop("state", None)

    if not flask.request.args:
        error = OAuthError.INVALID_REQUEST
        desc = "No arguments provided, request is invalid."
    elif state is None:
        error = OAuthError.INVALID_STATE
        desc = "State is not set, this page was probably refreshed."
    elif state != flask.request.args.get("state"):
        error = OAuthError.INVALID_STATE
        desc = "State does not match callback state."
    elif "error" in flask.request.args:
        error = oauth.normalize_error(
            flask.request.args["error"],
            allowed_types=oauth.AUTHORIZATION_ERRORS,
            fallback_type=OAuthError.SERVER_ERROR,
        )
        desc = error.description
    elif not flask.request.args.get("code"):
        error = OAuthError.INVALID_REQUEST
        desc = "Authorization code missing from provider callback."

    if error is not None:
        msg = f"Callback failed {error}: {desc}"

        # TODO: Consider just logging the request args as extra?
        if error == OAuthError.INVALID_SCOPE:
            msg += " - %r" % flask.request.args.get("scope")

        logger.log(
            current_settings.error_levels.get(error, LogLevel.ERROR),
            msg,
        )

        return _error(error, desc, client_state)

    result = oauth.fetch(
        current_settings.oauth.token_uri,
        client_id=current_settings.oauth.client_id,
        client_secret=current_settings.oauth.client_secret.get_secret_value(),
        code=flask.request.args.get("code"),
        grant_type="authorization_code",
        redirect_uri=current_settings.oauth.redirect_uri,
        endpoint="token",
    )

    if "error" in result:
        error = oauth.normalize_error(
            result["error"],
            allowed_types=oauth.TOKEN_ERRORS,
            fallback_type=OAuthError.SERVER_ERROR,
        )
        desc = error.description
    elif not oauth.validate_token(result):
        error = "invalid_response"
        desc = "Invalid response from provider."

    if error is not None:
        sanitized_result = oauth.sanitize_for_logging(result)
        logger.warning("Retrieving token failed", result=sanitized_result)

        current_span = trace.get_current_span()
        current_span.add_event("token_error", sanitized_result)

        return _error(error, desc, client_state, result.get("retry_after"))

    if "refresh_token" in result:
        result = oauth.scrub_refresh_token(result)

    client_secret = crypto.generate_key()
    token = crypto.dumps(client_secret, result)

    client_id = db.generate_id()
    telemetry.set_client_id(client_id)
    inserted_fields = tuple(sorted(result.keys()))
    logger.warning("Inserting token", inserted_fields=inserted_fields)
    trace.get_current_span().add_event(
        "Inserting token", {"inserted_fields": inserted_fields}
    )

    try:
        db.insert(client_id, token)
    except db.IntegrityError:
        logger.warning("Could not get unique client id.")
        return _error("integrity_error", "Database integrity error.", client_state)

    return _render(
        client_id=str(client_id), client_secret=client_secret, state=client_state
    )


@routes.route("/token", methods=["POST"])
def token() -> flask.Response:
    """Validate token request, refreshing when needed."""
    # TODO: allow all methods and raise invalid_request for !POST?

    if flask.request.form.get("grant_type") != "client_credentials":
        raise oauth.Error(
            OAuthError.UNSUPPORTED_GRANT_TYPE,
            'Only "client_credentials" is supported.',
        )
    elif "scope" in flask.request.form:
        raise oauth.Error(OAuthError.INVALID_SCOPE, "Setting scope is not supported.")

    try:
        # Trigger decoding base64 value that might have bad Unicode data.
        authorization: Any | None = flask.request.authorization
    except ValueError:
        authorization = None

    if authorization and authorization.type != "basic":
        raise oauth.Error(OAuthError.INVALID_CLIENT, "Only Basic Auth is supported.")

    client_id_value: str | None = flask.request.form.get("client_id")
    client_secret_value: str | None = flask.request.form.get("client_secret")
    if (client_id_value or client_secret_value) and authorization:
        raise oauth.Error(
            OAuthError.INVALID_REQUEST,
            "More than one mechanism for authenticating set.",
        )
    elif authorization:
        client_id_value = authorization.username
        client_secret_value = authorization.password

    try:
        credentials = client.validate_credentials(client_id_value, client_secret_value)
    except client.ClientIdValidationError:
        if client_id_value is not None:
            telemetry.record_invalid_client_id(client_id_value)
        raise oauth.Error(OAuthError.INVALID_CLIENT, "Malformed client_id.")
    except client.ClientSecretValidationError:
        raise oauth.Error(OAuthError.INVALID_CLIENT, "Client not known.")
    except client.CredentialValidationError as e:
        raise oauth.Error(OAuthError.INVALID_CLIENT, str(e))
    else:
        telemetry.set_client_id(credentials.client_id)

    client_id = credentials.client_id
    client_secret = credentials.client_secret

    try:
        record = db.lookup(client_id)
    except LookupError:
        raise oauth.Error(OAuthError.INVALID_CLIENT, "Client not known.")

    if record.encrypted_token is None:
        workaround_response = _revoked_grant_workaround_response()
        if workaround_response is not None:
            logger.warning("Serving revoked grant workaround token")
            stats.WorkaroundCounter.labels(workaround="revoked_grant").inc()
            trace.get_current_span().add_event("Served revoked grant workaround token")
            return flask.jsonify(workaround_response)

        raise oauth.Error(OAuthError.INVALID_GRANT, "Grant has been revoked.")

    try:
        result = crypto.loads(client_secret, record.encrypted_token)
    except (crypto.InvalidToken, TypeError, ValueError):
        # Always return same message as for client not found to avoid leaking
        # valid clients directly, timing attacks could of course still work.
        raise oauth.Error(OAuthError.INVALID_CLIENT, "Client not known.")

    if "refresh_token" not in result:
        stats.observe_token_grant_age(record.created_at)
        return flask.jsonify(result)

    refresh_result = oauth.fetch(
        current_settings.oauth.refresh_uri or current_settings.oauth.token_uri,
        client_id=current_settings.oauth.client_id,
        client_secret=current_settings.oauth.client_secret.get_secret_value(),
        grant_type=current_settings.oauth.grant_type,
        refresh_token=result["refresh_token"],
        endpoint="refresh",
    )
    refresh_outcome = oauth.token_endpoint_outcome(
        HTTPStatus.BAD_REQUEST if "error" in refresh_result else HTTPStatus.OK,
        refresh_result,
        retry_status_codes=current_settings.fetch.retry_status_codes,
        error_types=current_settings.fetch.error_types,
    )

    if "error" in refresh_result:
        error = refresh_outcome.normalized_error or OAuthError.SERVER_ERROR

        if refresh_outcome.invalidate_refresh_token:
            # Cache terminal refresh failures locally so older clients stop
            # repeatedly sending the same dead refresh token upstream.
            # Spotify refresh token expiry: https://developer.spotify.com/blog/2026-06-18-refresh-token-expiration
            db.update(client_id, None)
            stats.RefreshTokenInvalidationCounter.labels(reason=error.value).inc()
            logger.warning("Revoking stored token after upstream invalid_grant")
        elif error == OAuthError.TEMPORARILY_UNAVAILABLE:
            logger.warning(
                "Token refresh failed",
                refresh_result=oauth.sanitize_for_logging(refresh_result),
            )
        else:
            logger.error(
                "Token refresh failed",
                refresh_result=oauth.sanitize_for_logging(refresh_result),
            )

        current_span = trace.get_current_span()
        current_span.add_event(
            "refresh_error",
            oauth.sanitize_for_logging(refresh_result),
        )

        # Client Credentials access token responses use the same errors
        # as Authorization Code Grant access token responses. As such, just
        # raise the error we got.
        # TODO: Retry after header for error case?
        # This was the case where returning the retry-after from fetch could make sense.
        raise oauth.Error(
            error,
            refresh_result.get("error_description"),
            refresh_result.get("error_uri"),
            refresh_result.get("retry_after"),
        )

    if not oauth.validate_token(refresh_result):
        raise oauth.Error(OAuthError.INVALID_REQUEST, "Invalid response from provider.")

    # Copy over original scope if not set in refresh.
    if "scope" not in refresh_result and "scope" in result:
        refresh_result["scope"] = result["scope"]

    # Copy of stored db token to track if we need to update anything.
    modified = oauth.scrub_refresh_token(result)

    # Remove any new refresh_token and update DB with new value.
    if "refresh_token" in refresh_result:
        modified["refresh_token"] = refresh_result["refresh_token"]
        del refresh_result["refresh_token"]

    # Reduce write pressure by only issuing update on changes.
    if result != modified:
        updated_fields = _updated_fields(result, modified)
        logger.warning("Updating token", updated_fields=updated_fields)
        trace.get_current_span().add_event(
            "Updating token", {"updated_fields": updated_fields}
        )
        db.update(client_id, crypto.dumps(client_secret, modified))

    # Only return what we got from the API (minus refresh_token).
    stats.observe_token_grant_age(record.created_at)
    return flask.jsonify(refresh_result)


@routes.route("/metrics", methods=["GET"])
def metrics() -> flask.Response:
    if not current_settings.metrics_enabled:
        return flask.Response(status=HTTPStatus.NOT_FOUND)

    token = current_settings.metrics_token
    if token is not None:
        authorization = flask.request.headers.get("Authorization", "")
        expected = f"Bearer {token.get_secret_value()}"
        if not hmac.compare_digest(authorization, expected):
            return flask.Response(
                status=HTTPStatus.UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer"},
            )

    return stats.export_metrics()


def _error(
    error: OAuthError | str,
    description: str | None = None,
    state: str | None = None,
    retry_after: int | None = None,
) -> flask.Response:
    if error == OAuthError.INVALID_CLIENT:
        status = HTTPStatus.UNAUTHORIZED
    elif error == OAuthError.TEMPORARILY_UNAVAILABLE:
        status = HTTPStatus.SERVICE_UNAVAILABLE
    else:
        status = HTTPStatus.BAD_REQUEST

    if isinstance(error, OAuthError):
        description = description or error.description
        error_code = error.value
    else:
        error_code = error

    current_span = trace.get_current_span()
    current_span.set_status(
        trace.Status(trace.StatusCode.ERROR, f"{error_code}: {description}")
    )
    current_span.add_event(
        "error",
        {EXCEPTION_MESSAGE: f"{error_code}: {description}", EXCEPTION_TYPE: error_code},
    )

    stats.ServerErrorCounter.labels(
        endpoint=stats.endpoint(),
        status=stats.status(status),
        error=error,
    ).inc()

    response = _render(error=error_code, description=description, state=state)
    response.status_code = status
    if retry_after is not None and status == HTTPStatus.SERVICE_UNAVAILABLE:
        response.headers["Retry-After"] = int(retry_after)
    return response


def _revoked_grant_workaround_response() -> dict[str, Any] | None:
    user_agents = current_settings.revoked_grant_workaround_user_agents
    if not user_agents:
        return None

    user_agent = flask.request.user_agent.string
    if not user_agent or not re.search(user_agents, user_agent):
        return None

    return {
        "access_token": current_settings.revoked_grant_workaround_access_token,
        "token_type": "Bearer",
        "expires_in": current_settings.revoked_grant_workaround_expires_in,
    }


# TODO: Pass in the template string instead of settings.
def _render(
    client_id: str | None = None,
    client_secret: str | None = None,
    state: str | None = None,
    error: str | None = None,
    description: str | None = None,
) -> flask.Response:
    # Keep all the vars in something we can dump for tests with tojson.
    variables = {
        "client_id": client_id,
        "client_secret": client_secret,
        "state": state,
        "error": error,
        "description": description,
    }
    response = flask.Response(
        flask.render_template_string(
            current_settings.callback_template,
            variables=variables,
            **variables,
        ).encode("utf-8"),
        content_type="text/html; charset=UTF-8",
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if current_settings.callback_content_security_policy is not None:
        response.headers["Content-Security-Policy"] = (
            current_settings.callback_content_security_policy
        )
    return response
