"""Expose HTTPcore phase traces and setup counters without capturing wire data.

``httpx.connection.setup`` means this attempt observed socket setup, not that it
successfully connected. Its absence of setup is not proof of reuse for custom
transports. TCP setup includes DNS resolution; response-header waits include
network time as well as provider processing.

Connection-event counters run independently of trace sampling. TCP completion
counts connected sockets, not successful TLS handshakes or currently open
connections. Compare TCP starts with transport requests to measure setup churn,
not an exact reuse ratio: failed attempts can open multiple sockets per request.
"""

from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx as httpx_client
from opentelemetry import trace
from opentelemetry.instrumentation import (
    httpx,  # pyright: ignore[reportMissingModuleSource]
)

from . import _prometheus


async def request_hook(span: trace.Span, request: httpx.RequestInfo) -> None:
    _prometheus.ClientTransportRequestCounter.inc()
    extensions = request.extensions
    if extensions is None:
        return

    previous = cast(_TraceCallback | None, extensions.get("trace"))
    # HTTPX callers can send the same Request more than once.
    if isinstance(previous, _TransportTrace):
        previous = previous.previous
        if previous is None:
            extensions.pop("trace")
        else:
            extensions["trace"] = previous

    if span.is_recording():
        span.set_attribute("httpx.connection.setup", False)
    extensions["trace"] = _TransportTrace(span, previous)


async def response_hook(
    span: trace.Span, request: httpx.RequestInfo, response: httpx.ResponseInfo
) -> None:
    if not span.is_recording():
        return
    headers = httpx_client.Headers(response.headers)
    for name in ("content-length", "content-type", "retry-after"):
        value = headers.get(name)
        if value is not None:
            span.set_attribute(f"http.response.header.{name.replace('-', '_')}", value)


type _TraceCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

# HTTPcore's trace-event vocabulary is version-dependent. Keep the allowlist
# narrow: callback payloads contain requests, headers, streams, and exceptions.
_PHASES = frozenset(
    {
        "connection.connect_tcp",
        "connection.connect_unix_socket",
        "connection.start_tls",
        "http11.send_request_headers",
        "http11.send_request_body",
        "http11.receive_response_headers",
        "http2.send_request_headers",
        "http2.send_request_body",
        "http2.receive_response_headers",
    }
)
_CONNECT_PHASES = frozenset(
    {"connection.connect_tcp", "connection.connect_unix_socket"}
)
_CONNECTION_PHASES = {
    "connection.connect_tcp": "tcp",
    "connection.connect_unix_socket": "unix",
    "connection.start_tls": "tls",
}


class _TransportTrace:
    def __init__(self, parent: trace.Span, previous: _TraceCallback | None) -> None:
        self.previous = previous
        self._parent = parent
        self._context = trace.set_span_in_context(parent)
        self._spans: dict[str, trace.Span] = {}

    async def __call__(self, name: str, info: dict[str, Any]) -> None:
        self._record(name, info)
        if self.previous is not None:
            await self.previous(name, info)

    def _record(self, name: str, info: dict[str, Any]) -> None:
        phase, _, event = name.rpartition(".")
        if phase not in _PHASES or event not in {"started", "complete", "failed"}:
            return

        if phase in _CONNECTION_PHASES:
            _prometheus.ClientConnectionEventCounter.labels(
                phase=_CONNECTION_PHASES[phase], event=event
            ).inc()

        if not self._parent.is_recording():
            return

        if event == "started":
            if phase in _CONNECT_PHASES:
                self._parent.set_attribute("httpx.connection.setup", True)
            self._spans[phase] = trace.get_tracer(
                "oauthclientbridge.telemetry.httpx"
            ).start_span(f"HTTP {phase}", context=self._context)
        elif event in {"complete", "failed"}:
            span = self._spans.pop(phase, None)
            if span is not None:
                if event == "failed":
                    span.set_status(trace.StatusCode.ERROR)
                    error = info.get("exception")
                    if isinstance(error, BaseException):
                        span.set_attribute("error.type", type(error).__qualname__)
                span.end()
