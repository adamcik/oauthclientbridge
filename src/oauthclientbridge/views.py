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

from oauthclientbridge import crypto, db, oauth, sentry, stats
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import LogLevel, current_settings

logger: structlog.BoundLogger = structlog.get_logger()

routes = Blueprint("views", __name__)


@routes.route("/")
def authorize() -> flask.Response:
    """Store random state in session cookie and redirect to auth endpoint."""
    redirect_uri: str | None = flask.request.args.get("redirect_uri")
    if redirect_uri and redirect_uri != current_settings.oauth.redirect_uri:
        return _error(OAuthError.INVALID_REQUEST, "Wrong redirect_uri.")

    default_scope: str = " ".join(current_settings.oauth.scopes or [])
    scope = flask.request.args.get("scope", default_scope)
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

        level = current_settings.error_levels.get(error, "ERROR")
        level = logging.getLevelNamesMapping()[level]

        logger.log(level, msg)

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
        logger.warning("Retrieving token failed", result=result)

        current_span = trace.get_current_span()
        current_span.add_event("token_error", result)

        return _error(error, desc, client_state)

    if "refresh_token" in result:
        result = oauth.scrub_refresh_token(result)

    client_secret = crypto.generate_key()
    token = crypto.dumps(client_secret, result)

    client_id = db.generate_id()
    # TODO: Make this into telemetry.set_user and populate span attr?
    sentry.set_user({"client_id": client_id})

    try:
        db.insert(client_id, token)
    except db.IntegrityError:
        logger.warning("Could not get unique client id.")
        return _error("integrity_error", "Database integrity error.", client_state)

    return _render(client_id=client_id, client_secret=client_secret, state=client_state)


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

    client_id: str | None = flask.request.form.get("client_id")
    client_secret: str | None = flask.request.form.get("client_secret")
    if (client_id or client_secret) and authorization:
        raise oauth.Error(
            OAuthError.INVALID_REQUEST,
            "More than one mechanism for authenticating set.",
        )
    elif authorization:
        client_id = authorization.username
        client_secret = authorization.password

    if not client_id or not client_secret:
        raise oauth.Error(
            OAuthError.INVALID_CLIENT,
            "Both client_id and client_secret must be set.",
        )
    elif client_id == client_secret:
        raise oauth.Error(
            OAuthError.INVALID_CLIENT,
            "client_id and client_secret set to same value.",
        )

    # TODO: Combine this in telemetry.set_user() that also does span...
    structlog.contextvars.bind_contextvars(client_id=client_id)
    sentry.set_user({"client_id": client_id})

    try:
        token = db.lookup(client_id)
    except LookupError:
        raise oauth.Error(OAuthError.INVALID_CLIENT, "Client not known.")

    if token is None:
        # TODO: How do we avoid client retries here?
        raise oauth.Error(OAuthError.INVALID_GRANT, "Grant has been revoked.")

    try:
        result = crypto.loads(client_secret, token)
    except (crypto.InvalidToken, TypeError, ValueError):
        # Always return same message as for client not found to avoid leaking
        # valid clients directly, timing attacks could of course still work.
        raise oauth.Error(OAuthError.INVALID_CLIENT, "Client not known.")

    if "refresh_token" not in result:
        return flask.jsonify(result)

    refresh_result = oauth.fetch(
        current_settings.oauth.refresh_uri or current_settings.oauth.token_uri,
        client_id=current_settings.oauth.client_id,
        client_secret=current_settings.oauth.client_secret.get_secret_value(),
        grant_type=current_settings.oauth.grant_type,
        refresh_token=result["refresh_token"],
        endpoint="refresh",
    )

    if "error" in refresh_result:
        error = oauth.normalize_error(
            refresh_result["error"],
            allowed_types=oauth.TOKEN_ERRORS,
            fallback_type=OAuthError.SERVER_ERROR,
        )

        if error == OAuthError.INVALID_GRANT:
            # TODO: Store when we got an invalid grant? Or just cache this so
            # we have fewer backend calls to provider?

            # NOTE: This was commented out to avoid invalidating things in case
            # something went wrong upstream.
            # db.update(client_id, None)
            logger.warning("Invalid grant")
        elif error == OAuthError.TEMPORARILY_UNAVAILABLE:
            logger.warning("Token refresh failed", refresh_result=refresh_result)
        else:
            logger.error("Token refresh failed", refresh_result=refresh_result)

        current_span = trace.get_current_span()
        current_span.add_event("refresh_error", refresh_result)

        # Client Credentials access token responses use the same errors
        # as Authorization Code Grant access token responses. As such, just
        # raise the error we got.
        # TODO: Retry after header for error case?
        # This was the case where returning the retry-after from fetch could make sense.
        raise oauth.Error(
            error,
            refresh_result.get("error_description"),
            refresh_result.get("error_uri"),
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
        logger.warning("Updating token")
        db.update(client_id, crypto.dumps(client_secret, modified))

    # Only return what we got from the API (minus refresh_token).
    return flask.jsonify(refresh_result)


@routes.route("/metrics", methods=["GET"])
def metrics() -> flask.Response:
    return stats.export_metrics()


def _error(
    error: OAuthError | str,
    description: str | None = None,
    state: str | None = None,
) -> flask.Response:
    if error == OAuthError.INVALID_CLIENT:
        status = HTTPStatus.UNAUTHORIZED
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
        {EXCEPTION_MESSAGE: description or "", EXCEPTION_TYPE: error_code},
    )

    stats.ServerErrorCounter.labels(
        endpoint=stats.endpoint(),
        status=stats.status(status),
        error=error,
    ).inc()

    response = _render(error=error_code, description=description, state=state)
    response.status_code = status
    return response


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
    return flask.Response(
        flask.render_template_string(
            current_settings.callback_template,
            variables=variables,
            **variables,
        ).encode("utf-8"),
        content_type="text/html; charset=UTF-8",
    )
