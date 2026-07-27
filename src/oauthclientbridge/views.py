import hmac
from collections.abc import Awaitable, Callable
from functools import partial
from http import HTTPStatus
from typing import ParamSpec, cast

import anyio
import flask
import structlog
from flask import Blueprint
from werkzeug.exceptions import InternalServerError

from oauthclientbridge import bridge, endpoint_execution, observer, telemetry, types
from oauthclientbridge.settings import Settings, current_settings

routes = Blueprint("views", __name__)
P = ParamSpec("P")


@routes.route("/")
def authorize() -> flask.Response:
    """Store random state in session cookie and redirect to auth endpoint."""
    return _run(
        types.Endpoint.AUTHORIZE, _get_bridge().authorize, query=flask.request.args
    )


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
    return _run(
        types.Endpoint.CALLBACK,
        _get_bridge().callback,
        query=flask.request.args,
        session=dict(flask.session),
    )


def _get_bridge() -> bridge.Bridge:
    return cast(bridge.Bridge, flask.current_app.extensions["oauth_bridge"])


def _get_settings() -> Settings:
    return cast(Settings, flask.current_app.config["SETTINGS"])


def _get_fallback_observer() -> observer.FallbackObserver:
    return cast(
        observer.FallbackObserver,
        flask.current_app.extensions["oauth_fallback_observer"],
    )


def _get_outcome_observer() -> observer.OAuthOutcomeObserver:
    return cast(
        observer.OAuthOutcomeObserver,
        flask.current_app.extensions["oauth_outcome_observer"],
    )


def _flask_response_with_context(
    result: endpoint_execution.EndpointResult,
) -> flask.Response:
    structlog.contextvars.bind_contextvars(**result.log_context)
    return _flask_response(result.response)


@routes.route("/token", methods=["POST"])
def token() -> flask.Response:
    """Validate token request, refreshing when needed."""
    return _run(
        types.Endpoint.TOKEN,
        _get_bridge().token,
        form=flask.request.form,
        authorization=flask.request.headers.get("Authorization"),
        user_agent=flask.request.user_agent.string,
    )


def _run(
    endpoint: types.Endpoint,
    operation: Callable[P, Awaitable[bridge.BridgeResult]],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> flask.Response:
    """Run a Bridge endpoint through shared execution and adapt it to Flask."""
    result = anyio.run(
        partial(
            endpoint_execution.run,
            _get_settings(),
            _get_fallback_observer(),
            _get_outcome_observer(),
            endpoint,
            operation,
            *args,
            **kwargs,
        )
    )
    return _flask_response_with_context(result)


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

    try:
        return telemetry.export_metrics()
    except Exception as exception:
        return _fallback(types.Endpoint.METRICS, exception)


def fallback_error_handler(exception: Exception) -> flask.Response:
    """Adapt escaped application faults to the shared unknown fallback."""
    if isinstance(exception, InternalServerError) and exception.original_exception:
        return _fallback(types.Endpoint.UNKNOWN, exception.original_exception)
    return _fallback(types.Endpoint.UNKNOWN, exception)


def _fallback(endpoint: types.Endpoint, exception: BaseException) -> flask.Response:
    response = endpoint_execution.fallback(
        _get_settings(),
        _get_fallback_observer(),
        endpoint,
        exception,
    )
    return _flask_response(response)
