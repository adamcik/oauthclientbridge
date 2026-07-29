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

from oauthclientbridge import observer, types

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
                f"{HTTP_RESPONSE_HEADER_TEMPLATE}.content_length": response.content_length,
                f"{HTTP_RESPONSE_HEADER_TEMPLATE}.content_type": response.content_type,
                f"{HTTP_RESPONSE_HEADER_TEMPLATE}.cache_control": response.cache_control,
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
                AccessLogFormatter().format(self._access_log_format, **attributes),
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
