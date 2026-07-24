import hmac
import re
from http import HTTPStatus
from typing import Any, cast

import anyio
import flask
import structlog
from flask import Blueprint
from opentelemetry import trace

from oauthclientbridge import bridge, client, crypto, db, oauth, telemetry
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import current_settings

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
    response = anyio.run(lambda: _get_bridge().authorize(query=flask.request.args))
    return _flask_response(response)


def _flask_response(bridge_response: bridge.BridgeResponse) -> flask.Response:
    if bridge_response.session is not None:
        flask.session.clear()
        flask.session.update(bridge_response.session)
    if isinstance(bridge_response.body, bytes):
        response = flask.Response(
            bridge_response.body,
            status=bridge_response.status,
            headers=bridge_response.headers,
        )
        return response
    response = flask.jsonify(bridge_response.body)
    response.status_code = bridge_response.status
    response.headers.update(bridge_response.headers)
    return response


@routes.route("/callback")
def callback() -> flask.Response:
    """Validate callback and trade in code for a token."""
    response = anyio.run(
        lambda: _get_bridge().callback(
            query=flask.request.args, session=dict(flask.session)
        )
    )
    return _flask_response(response)


def _get_bridge() -> bridge.Bridge:
    return cast(bridge.Bridge, flask.current_app.extensions["oauth_bridge"])


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
            telemetry.record_workaround_metric("revoked_grant")
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
        telemetry.observe_token_grant_age(record.created_at)
        return flask.jsonify(result)

    refresh_result = anyio.run(
        lambda: oauth.fetch(
            current_settings.oauth.refresh_uri or current_settings.oauth.token_uri,
            client_id=current_settings.oauth.client_id,
            client_secret=current_settings.oauth.client_secret.get_secret_value(),
            grant_type=current_settings.oauth.grant_type,
            refresh_token=result["refresh_token"],
            endpoint="refresh",
        )
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
            telemetry.record_refresh_token_invalidation_metric(error.value)
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
    telemetry.observe_token_grant_age(record.created_at)
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

    return telemetry.export_metrics()


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
