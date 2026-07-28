from typing import cast

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from oauthclientbridge import endpoint_execution, types
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
        query=dict(request.query_params),
    )
    return _response(request, result)


async def callback(request: Request) -> Response:
    context = _context(request)
    result = await endpoint_execution.run(
        context.settings,
        context.fallback_observer,
        context.outcome_observer,
        types.Endpoint.CALLBACK,
        context.oauth_bridge.callback,
        query=dict(request.query_params),
        session=_session(request),
    )
    return _response(request, result)


async def token(request: Request) -> Response:
    form = await request.form()
    context = _context(request)
    result = await endpoint_execution.run(
        context.settings,
        context.fallback_observer,
        context.outcome_observer,
        types.Endpoint.TOKEN,
        context.oauth_bridge.token,
        form={key: value for key, value in form.items() if isinstance(value, str)},
        authorization=request.headers.get("Authorization"),
        user_agent=request.headers.get("User-Agent", ""),
    )
    return _response(request, result)


routes: list[BaseRoute] = [
    Route("/", authorize, methods=["GET"]),
    Route("/callback", callback, methods=["GET"]),
    Route("/token", token, methods=["POST"]),
]


def _response(
    request: Request, result: endpoint_execution.EndpointResult
) -> Response:
    structlog.contextvars.bind_contextvars(**result.log_context)
    response = result.response
    if response.session is not None:
        if response.session:
            request.session[_SESSION_KEY] = response.session
        else:
            request.session.pop(_SESSION_KEY, None)
    if isinstance(response.body, bytes):
        return Response(response.body, status_code=response.status, headers=response.headers)
    return JSONResponse(response.body, status_code=response.status, headers=response.headers)


def _session(request: Request) -> dict[str, str]:
    session = request.session.get(_SESSION_KEY, {})
    if not isinstance(session, dict):
        return {}
    return {key: value for key, value in session.items() if isinstance(value, str)}


def _context(request: Request) -> AppContext:
    return cast(AppContext, request.app.state.context)
