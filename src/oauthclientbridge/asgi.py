from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from oauthclientbridge import bridge, oauth, observer, telemetry, types
from oauthclientbridge.asgi_context import AppContext
from oauthclientbridge.routes import fallback, routes
from oauthclientbridge.settings import Settings
from oauthclientbridge.utils import uri


class RequestLifecycleMiddleware:
    """Adapt ASGI messages to the shared request lifecycle observer."""

    def __init__(
        self, app: ASGIApp, *, lifecycle_observer: observer.RequestLifecycleObserver
    ) -> None:
        self.app = app
        self.lifecycle_observer = lifecycle_observer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        self.lifecycle_observer.start(_request(scope))
        response: observer.RequestLifecycleResponse | None = None
        response_size = 0

        async def observe(message: Message) -> None:
            nonlocal response, response_size
            if message["type"] == "http.response.start":
                headers = _headers(message["headers"])
                response = observer.RequestLifecycleResponse(
                    status_code=message["status"],
                    body_size=None,
                    content_type=headers.get("content-type"),
                    content_length=_content_length(headers),
                    cache_control=headers.get("cache-control"),
                )
            elif message["type"] == "http.response.body":
                response_size += len(message.get("body", b""))
                if response is not None and not message.get("more_body", False):
                    self.lifecycle_observer.complete(
                        _endpoint(scope, response.status_code),
                        observer.RequestLifecycleResponse(
                            status_code=response.status_code,
                            body_size=response_size,
                            content_type=response.content_type,
                            content_length=response.content_length,
                            cache_control=response.cache_control,
                        ),
                    )
            await send(message)

        await self.app(scope, receive, observe)


def _request(scope: Scope) -> observer.RequestLifecycleRequest:
    headers = _headers(scope["headers"])
    scheme = scope.get("scheme", "http")
    server = scope.get("server") or ("localhost", 80)
    host = headers.get("host", str(server[0]))
    path = scope["path"]
    query = scope["query_string"].decode("ascii", "replace")
    url = uri.sanitize_url(f"{scheme}://{host}{path}?{query}")
    sanitized_query = url.split("?", 1)[1] if url is not None and "?" in url else ""
    client = scope.get("client")
    return observer.RequestLifecycleRequest(
        attributes={
            "client.address": client[0] if client is not None else None,
            "http.request.method": scope["method"],
            "http.request.body.size": _content_length(headers),
            "http.route": None,
            "network.protocol.version": scope.get("http_version"),
            "server.address": host,
            "url.full": url,
            "url.path": path,
            "url.query": sanitized_query,
            "url.scheme": scheme,
            "user_agent.original": headers.get("user-agent"),
            "http.request.header.content_type": headers.get("content-type"),
            "http.request.header.content_length": _content_length(headers),
        }
    )


def _endpoint(scope: Scope, status_code: int) -> types.Endpoint:
    if status_code in {404, 405}:
        return types.Endpoint.UNKNOWN
    endpoint = scope.get("endpoint")
    name = getattr(endpoint, "__name__", None)
    endpoints = {
        "authorize": types.Endpoint.AUTHORIZE,
        "callback": types.Endpoint.CALLBACK,
        "token": types.Endpoint.TOKEN,
        "metrics": types.Endpoint.METRICS,
    }
    return (
        endpoints.get(name, types.Endpoint.UNKNOWN)
        if isinstance(name, str)
        else types.Endpoint.UNKNOWN
    )


def _headers(raw_headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in raw_headers
    }


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = headers.get("content-length")
    return int(value) if value is not None and value.isdecimal() else None


def create_app(
    settings: Settings,
    *,
    fetch: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    fallback_observer: observer.FallbackObserver | None = None,
    outcome_observer: observer.OAuthOutcomeObserver | None = None,
) -> Starlette:
    """Create the in-process Starlette adapter without starting an ASGI runtime."""
    if settings.session_secret is None:
        raise ValueError("BRIDGE_SESSION_SECRET must be set for the ASGI adapter")

    oauth_bridge = bridge.Bridge(settings, fetch or oauth.fetch_for(settings.fetch))
    fallback_observer = fallback_observer or telemetry.fallback_observer()
    outcome_observer = outcome_observer or telemetry.oauth_outcome_observer()

    app = Starlette(routes=routes, exception_handlers={Exception: fallback})
    app.state.context = AppContext(
        settings=settings,
        oauth_bridge=oauth_bridge,
        fallback_observer=fallback_observer,
        outcome_observer=outcome_observer,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
    )
    app.add_middleware(
        RequestLifecycleMiddleware,
        lifecycle_observer=telemetry.request_lifecycle_observer(
            settings.log.access_log_format
        ),
    )
    telemetry.instrument_asgi_app(app)
    return app
