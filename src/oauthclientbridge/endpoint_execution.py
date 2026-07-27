from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import ParamSpec

import structlog

from oauthclientbridge import bridge, observer, types
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import Settings

P = ParamSpec("P")
logger: structlog.BoundLogger = structlog.get_logger()


@dataclass(frozen=True)
class EndpointResult:
    """A framework-neutral response and bounded context for its adapter.

    Framework access logging runs after this asynchronous function returns to
    the adapter's execution context. The adapter binds ``log_context`` before
    converting ``response`` to its native response type.
    """

    response: bridge.BridgeResponse
    log_context: dict[str, str]


async def run(
    settings: Settings,
    fallback_observer: observer.FallbackObserver,
    outcome_observer: observer.OAuthOutcomeObserver,
    endpoint: types.Endpoint,
    operation: Callable[P, Awaitable[bridge.BridgeResult]],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> EndpointResult:
    """Execute one endpoint with shared outcome and fallback policy.

    The adapter supplies the stable endpoint identity. Bridge supplies a
    completed response and optional OAuth error classification. If Bridge
    raises before constructing a result, this function observes the original
    exception and returns that endpoint's safe fallback response instead.

    Observer failures are deliberately ignored: observability must not alter a
    client response, including when tests provide a failing observer.
    """
    try:
        result = await operation(*args, **kwargs)
    except Exception as exception:
        _observe_fallback(fallback_observer, endpoint, exception)
        response = _fallback_response(settings, endpoint)
        _observe_outcome(outcome_observer, endpoint, response, OAuthError.SERVER_ERROR)
        return EndpointResult(
            response=response,
            log_context={
                "oauth_endpoint": endpoint.value,
                "oauth_error": OAuthError.SERVER_ERROR.value,
            },
        )

    _observe_outcome(outcome_observer, endpoint, result.response, result.oauth_error)
    log_context = {"oauth_endpoint": endpoint.value}
    if result.oauth_error is not None:
        log_context["oauth_error"] = result.oauth_error.value
    return EndpointResult(response=result.response, log_context=log_context)


def _observe_fallback(
    fallback_observer: observer.FallbackObserver,
    endpoint: types.Endpoint,
    exception: Exception,
) -> None:
    try:
        fallback_observer.observe(endpoint, exception)
    except Exception:
        logger.exception("Fallback observer failed", endpoint=endpoint.value)
        return


def _observe_outcome(
    outcome_observer: observer.OAuthOutcomeObserver,
    endpoint: types.Endpoint,
    response: bridge.BridgeResponse,
    error: OAuthError | None,
) -> None:
    try:
        outcome_observer.observe(endpoint, response.status, error)
    except Exception:
        logger.exception(
            "OAuth outcome observer failed",
            endpoint=endpoint.value,
            oauth_error=error.value if error is not None else None,
        )
        return


def _fallback_response(
    settings: Settings, endpoint: types.Endpoint
) -> bridge.BridgeResponse:
    if endpoint in {types.Endpoint.AUTHORIZE, types.Endpoint.CALLBACK}:
        return bridge.render_browser_oauth_result(
            settings.callback_template,
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            content_security_policy=settings.callback_content_security_policy,
            error=OAuthError.SERVER_ERROR.value,
            description=OAuthError.SERVER_ERROR.description,
        )
    return bridge.BridgeResponse(
        status=HTTPStatus.INTERNAL_SERVER_ERROR,
        body=OAuthError.SERVER_ERROR.json(),
    )
