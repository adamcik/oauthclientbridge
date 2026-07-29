import string
import time
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from http import HTTPStatus
from typing import Any

import structlog
from opentelemetry.semconv.attributes.http_attributes import (
    HTTP_RESPONSE_HEADER_TEMPLATE,
    HTTP_RESPONSE_STATUS_CODE,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from oauthclientbridge import observer, types
from oauthclientbridge.utils import uri

from . import _prometheus

_request: ContextVar[observer.RequestLifecycleRequest | None] = ContextVar(
    "request_lifecycle_request", default=None
)
_started_at: ContextVar[int | None] = ContextVar(
    "request_lifecycle_started_at", default=None
)
_access_logger: structlog.BoundLogger = structlog.get_logger("oauthclientbridge.http")

HTTP_SERVER_DURATION = "http.server.duration"
HTTP_REQUEST_BODY_SIZE = "http.request.body.size"
HTTP_RESPONSE_BODY_SIZE = "http.response.body.size"


class RequestLifecycleObserver:
    """Records one framework-neutral request lifecycle without affecting it."""

    def __init__(self, access_log_format: str) -> None:
        self._access_log_format = access_log_format
        self._formatter = AccessLogFormatter()

    def start(self, request: observer.RequestLifecycleRequest) -> None:
        try:
            structlog.contextvars.clear_contextvars()
            _request.set(request)
            _started_at.set(time.perf_counter_ns())
        except Exception:
            return

    def complete(
        self, endpoint: types.Endpoint, response: observer.RequestLifecycleResponse
    ) -> None:
        try:
            request = _request.get()
            started_at = _started_at.get()
            if request is None or started_at is None:
                return

            duration = (time.perf_counter_ns() - started_at) / 1e9
            attributes = {
                **request.attributes,
                HTTP_SERVER_DURATION: duration,
                HTTP_RESPONSE_BODY_SIZE: response.body_size,
                HTTP_RESPONSE_STATUS_CODE: response.status_code,
                f"{HTTP_RESPONSE_HEADER_TEMPLATE}.content_length": response.headers.content_length,
                f"{HTTP_RESPONSE_HEADER_TEMPLATE}.content_type": response.headers.content_type,
                f"{HTTP_RESPONSE_HEADER_TEMPLATE}.cache_control": response.headers.cache_control,
            }
            labels = {
                "endpoint": endpoint.value,
                "status": _prometheus.status(HTTPStatus(response.status_code)),
            }
            _prometheus.ServerLatencyHistogram.labels(**labels).observe(duration)
            if response.body_size is not None:
                _prometheus.ServerResponseSizeHistogram.labels(**labels).observe(
                    response.body_size
                )
            request_size = request.attributes.get(HTTP_REQUEST_BODY_SIZE)
            if isinstance(request_size, int):
                _prometheus.ServerRequestSizeHistogram.labels(**labels).observe(
                    request_size
                )
            _access_logger.info(
                self._formatter.format(self._access_log_format, **attributes),
                **attributes,
            )
        except Exception:
            return
        finally:
            _request.set(None)
            _started_at.set(None)
            structlog.contextvars.clear_contextvars()


class AccessLogFormatter(string.Formatter):
    def get_field(
        self, field_name: str, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> tuple[Any, str]:
        if field_name in kwargs:
            value = kwargs[field_name]
            return value if value is not None else "-", field_name
        return "{" + field_name + "}", field_name


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

        self.lifecycle_observer.start(_asgi_request(scope))
        response: observer.RequestLifecycleResponse | None = None
        response_size = 0

        async def observe(message: Message) -> None:
            nonlocal response, response_size
            if message["type"] == "http.response.start":
                headers = _headers(message["headers"])
                response = observer.RequestLifecycleResponse(
                    status_code=message["status"],
                    body_size=None,
                    headers=observer.RequestLifecycleResponseHeaders(
                        content_type=headers.get("content-type"),
                        content_length=_content_length(headers),
                        cache_control=headers.get("cache-control"),
                    ),
                )
            elif message["type"] == "http.response.body":
                response_size += len(message.get("body", b""))
                if response is not None and not message.get("more_body", False):
                    self.lifecycle_observer.complete(
                        _asgi_endpoint(scope, response.status_code),
                        observer.RequestLifecycleResponse(
                            status_code=response.status_code,
                            body_size=response_size,
                            headers=response.headers,
                        ),
                    )
            await send(message)

        await self.app(scope, receive, observe)


def _asgi_request(scope: Scope) -> observer.RequestLifecycleRequest:
    headers = _headers(scope["headers"])
    scheme = scope.get("scheme", "http")
    server = scope.get("server") or ("localhost", 80)
    host = _bounded(headers.get("host", str(server[0])))
    path = _bounded(scope["path"])
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
            "network.protocol.version": _bounded(scope.get("http_version")),
            "server.address": host,
            "url.full": url,
            "url.path": path,
            "url.query": sanitized_query,
            "url.scheme": scheme,
            "user_agent.original": _bounded(headers.get("user-agent")),
            "http.request.header.content_type": _bounded(headers.get("content-type")),
            "http.request.header.content_length": _content_length(headers),
        }
    )


def _asgi_endpoint(scope: Scope, status_code: int) -> types.Endpoint:
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


def _bounded(value: str | None) -> str | None:
    return value[:1024] if value is not None else None
