import hmac
from http import HTTPStatus
from typing import cast

import anyio
import flask
import structlog
from flask import Blueprint
from opentelemetry import trace

from oauthclientbridge import bridge, oauth, telemetry
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import current_settings

routes = Blueprint("views", __name__)


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
    response = anyio.run(
        lambda: _get_bridge().token(
            form=flask.request.form,
            authorization=flask.request.headers.get("Authorization"),
            user_agent=flask.request.user_agent.string,
        )
    )
    _record_handled_token_error(response)
    return _flask_response(response)


def _record_handled_token_error(response: bridge.BridgeResponse) -> None:
    if not isinstance(response.body, dict):
        return
    error_code = response.body.get("error")
    description = response.body.get("error_description")
    if not isinstance(error_code, str) or not isinstance(description, str):
        return
    if error_code not in OAuthError:
        return

    error = oauth.Error(OAuthError(error_code), description)
    current_span = trace.get_current_span()
    current_span.set_attribute("error.unhandled", False)
    current_span.set_attribute("oauth.error", error_code)
    structlog.contextvars.bind_contextvars(oauth_error=error_code)
    current_span.record_exception(error)
    current_span.set_status(trace.Status(trace.StatusCode.ERROR, str(error)))
    telemetry.record_server_error_metric(response.status, error_code)


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
