import hmac
from http import HTTPStatus
from typing import cast

import anyio
import flask
from flask import Blueprint

from oauthclientbridge import bridge, telemetry
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
    return _flask_response(response)


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
