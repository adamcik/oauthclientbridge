from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from http import HTTPStatus
from typing import Literal, Protocol

from oauthclientbridge import types
from oauthclientbridge.errors import OAuthError

from . import (
    _asyncio,
    _fallback,
    _lifecycle,
    _oauth_outcome,
    _otel,
    _prometheus,
    _refresh,
    _resources,
    _sentry,
)

__all__ = [
    "add_refresher",
    "export_metrics",
    "fallback_observer",
    "init_metrics",
    "init_sentry",
    "init_tracing",
    "instrument",
    "instrument_app",
    "instrument_asgi_app",
    "observe_token_grant_age_metric",
    "oauth_outcome_observer",
    "record_client_attempt_metric",
    "record_client_error_metric",
    "record_client_response_metric",
    "record_client_retries_metric",
    "record_database_error_metric",
    "record_database_latency_metric",
    "record_oauth_error_trace",
    "bind_invalid_client_id_log_context",
    "capture_unhandled_exception",
    "record_invalid_client_id_trace",
    "record_refresh_token_invalidation_metric",
    "record_retry_decision_metric",
    "record_server_error_metric",
    "record_workaround_metric",
    "request_lifecycle_observer",
    "request_refresh",
    "set_build_info_metric",
    "set_client_id_context",
    "set_token_state_counts_metric",
    "start_asyncio_monitor",
    "start_background_refresh",
    "stop_background_refresh",
    "uninstrument",
    "otel_log_attributes",
]

set_client_id_context = _otel.set_client_id_context
bind_invalid_client_id_log_context = _otel.bind_invalid_client_id_log_context
capture_unhandled_exception = _sentry.capture_unhandled_exception
record_invalid_client_id_trace = _otel.record_invalid_client_id_trace
record_oauth_error_trace = _otel.record_oauth_error_trace
instrument = _otel.instrument
uninstrument = _otel.uninstrument
instrument_app = _otel.instrument_app
instrument_asgi_app = _otel.instrument_asgi_app
init_tracing = _otel.init_tracing
init_metrics = _otel.init_metrics
init_sentry = _sentry.init
oauth_outcome_observer = _oauth_outcome.OAuthOutcomeObserver
fallback_observer = _fallback.FallbackObserver

type ClientErrorLabel = OAuthError | Literal["invalid_error", "pool_saturation"]
type ClientResponseStatus = Literal[
    "connection_error",
    "connection_timeout",
    "proxy_error",
    "read_timeout",
    "ssl_error",
    "unknown_exception",
]
type Workaround = Literal["revoked_grant"]

otel_log_attributes = _resources.otel_log_attributes

export_metrics = _prometheus.export_metrics
request_lifecycle_observer = _lifecycle.RequestLifecycleObserver
observe_token_grant_age_metric = _prometheus.observe_token_grant_age_metric
set_build_info_metric = _prometheus.set_build_info_metric
set_token_state_counts_metric = _prometheus.set_token_state_counts_metric
add_refresher = _refresh.add_refresher
request_refresh = _refresh.request_refresh
start_background_refresh = _refresh.start_background_refresh
stop_background_refresh = _refresh.stop_background_refresh


class _TaskSpawner(Protocol):
    def start_soon(self, func: Callable[[], Awaitable[None]]) -> None: ...


def start_asyncio_monitor(task_spawner: _TaskSpawner, name: str) -> None:
    """Start event-loop health monitoring in an application task group."""
    monitor = _asyncio.AsyncioMonitor(name)
    task_spawner.start_soon(monitor.run)


def record_database_latency_metric(
    name: types.DatabaseOperation,
) -> AbstractContextManager[object]:
    return _prometheus.DBLatencyHistorgram.labels(query=name).time()


def record_database_error_metric(
    name: types.DatabaseOperation, error: types.DatabaseError
) -> None:
    _prometheus.DBErrorCounter.labels(query=name, error=error).inc()


def record_server_error_metric(
    status: HTTPStatus, error: OAuthError, endpoint: types.Endpoint | None = None
) -> None:
    _prometheus.ServerErrorCounter.labels(
        endpoint=endpoint or "unknown",
        status=_prometheus.status(status),
        error=error,
    ).inc()


def record_client_attempt_metric(
    endpoint: types.UpstreamGrantType, kind: types.RetryAttemptKind
) -> None:
    _prometheus.ClientAttemptCounter.labels(endpoint=endpoint, kind=kind).inc()


def record_retry_decision_metric(
    endpoint: types.UpstreamGrantType,
    decision: types.RetryDecisionAction,
    reason: types.RetryCondition,
) -> None:
    _prometheus.ClientRetryDecisionCounter.labels(
        endpoint=endpoint, decision=decision, reason=reason
    ).inc()


def record_client_error_metric(
    endpoint: types.UpstreamGrantType,
    status: HTTPStatus | None,
    error: ClientErrorLabel,
) -> None:
    _prometheus.ClientErrorCounter.labels(
        endpoint=endpoint,
        status=_prometheus.status(status) if status else "unknown",
        error=error,
    ).inc()


def record_client_retries_metric(
    endpoint: types.UpstreamGrantType, status: HTTPStatus | None, count: int
) -> None:
    _prometheus.ClientRetryHistogram.labels(
        endpoint=endpoint,
        status=_prometheus.status(status) if status else "unknown",
    ).observe(count)


def record_client_response_metric(
    endpoint: types.UpstreamGrantType,
    status: HTTPStatus | ClientResponseStatus,
    duration: float,
    size: int | None,
) -> None:
    status_label = (
        _prometheus.status(status) if isinstance(status, HTTPStatus) else status
    )
    labels = {"endpoint": endpoint, "status": status_label}
    if size is not None:
        _prometheus.ClientResponseSizeHistogram.labels(**labels).observe(size)
    _prometheus.ClientLatencyHistogram.labels(**labels).observe(duration)


def record_refresh_token_invalidation_metric(reason: OAuthError) -> None:
    _prometheus.RefreshTokenInvalidationCounter.labels(reason=reason).inc()


def record_workaround_metric(workaround: Workaround) -> None:
    _prometheus.WorkaroundCounter.labels(workaround=workaround).inc()
