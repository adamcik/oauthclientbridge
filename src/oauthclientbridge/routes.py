from typing import cast

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from oauthclientbridge import bridge, endpoint_execution, types
from oauthclientbridge.asgi_context import AppContext

_SESSION_KEY = "oauthclientbridge"


async def authorize(request: Request) -> Response:
    context = _context(request)
    result = await endpoint_execution.run(
        context.settings,
        context.fallback_observer,
        context.outcome_observer,
        types.Endpoint.AUTHORIZE,
        context.oauth_bridge.authorize,
        query=request.query_params,
    )
    return _endpoint_response(request, result)


async def callback(request: Request) -> Response:
    context = _context(request)
    result = await endpoint_execution.run(
        context.settings,
        context.fallback_observer,
        context.outcome_observer,
        types.Endpoint.CALLBACK,
        context.oauth_bridge.callback,
        query=request.query_params,
        session=_session(request),
    )
    return _endpoint_response(request, result)


async def token(request: Request) -> Response:
    context = _context(request)

    async def operation() -> bridge.BridgeResult:
        form = await request.form()
        return await context.oauth_bridge.token(
            form={key: value for key, value in form.items() if isinstance(value, str)},
            authorization=request.headers.get("Authorization"),
            user_agent=request.headers.get("User-Agent", ""),
        )

    result = await endpoint_execution.run(
        context.settings,
        context.fallback_observer,
        context.outcome_observer,
        types.Endpoint.TOKEN,
        operation,
    )
    return _endpoint_response(request, result)


routes: list[BaseRoute] = [
    Route("/", authorize, methods=["GET"]),
    Route("/callback", callback, methods=["GET"]),
    Route("/token", token, methods=["POST"]),
]


async def fallback(request: Request, exception: Exception) -> Response:
    context = _context(request)
    return _response(
        request,
        endpoint_execution.fallback(
            context.settings,
            context.fallback_observer,
            types.Endpoint.UNKNOWN,
            exception,
        ),
    )


def _endpoint_response(
    request: Request, result: endpoint_execution.EndpointResult
) -> Response:
    structlog.contextvars.bind_contextvars(**result.log_context)
    return _response(request, result.response)


def _response(request: Request, response: bridge.BridgeResponse) -> Response:
    if response.session is not None:
        if response.session:
            request.session[_SESSION_KEY] = response.session
        else:
            request.session.pop(_SESSION_KEY, None)
    if isinstance(response.body, bytes):
        return Response(
            response.body, status_code=response.status, headers=response.headers
        )
    return JSONResponse(
        response.body, status_code=response.status, headers=response.headers
    )


def _session(request: Request) -> dict[str, str]:
    session = request.session.get(_SESSION_KEY, {})
    if not isinstance(session, dict):
        return {}
    return {
        key: value
        for key, value in cast(dict[object, object], session).items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _context(request: Request) -> AppContext:
    return cast(AppContext, request.app.state.context)
