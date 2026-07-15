from contextlib import AbstractContextManager
from http import HTTPStatus

from . import _otel, _prometheus, _resources

__all__ = [
    "add_refresher",
    "export_metrics",
    "finalize_request_metrics",
    "init_metrics",
    "init_tracing",
    "instrument",
    "instrument_app",
    "observe_token_grant_age",
    "record_client_attempt",
    "record_client_error",
    "record_client_response",
    "record_client_retries",
    "record_database_error",
    "record_database_latency",
    "record_invalid_client_id",
    "record_refresh_token_invalidation",
    "record_request_metrics",
    "record_retry_decision",
    "record_server_error",
    "record_workaround",
    "request_refresh",
    "set_build_info",
    "set_client_id",
    "set_token_state_counts",
    "start_background_refresh",
    "stop_background_refresh",
    "uninstrument",
    "log_attributes",
    "runtime_log_attributes",
]

set_client_id = _otel.set_client_id
record_invalid_client_id = _otel.record_invalid_client_id
instrument = _otel.instrument
uninstrument = _otel.uninstrument
instrument_app = _otel.instrument_app
init_tracing = _otel.init_tracing
init_metrics = _otel.init_metrics

log_attributes = _resources.log_attributes
runtime_log_attributes = _resources.runtime_log_attributes

record_request_metrics = _prometheus.record_metrics
finalize_request_metrics = _prometheus.finalize_metrics
export_metrics = _prometheus.export_metrics
observe_token_grant_age = _prometheus.observe_token_grant_age
set_build_info = _prometheus.set_build_info
set_token_state_counts = _prometheus.set_token_state_counts
add_refresher = _prometheus.add_refresher
request_refresh = _prometheus.request_refresh
start_background_refresh = _prometheus.start_background_refresh
stop_background_refresh = _prometheus.stop_background_refresh


def record_database_latency(name: str) -> AbstractContextManager[object]:
    return _prometheus.DBLatencyHistorgram.labels(query=name).time()


def record_database_error(name: str, error: str) -> None:
    _prometheus.DBErrorCounter.labels(query=name, error=error).inc()


def record_server_error(status: HTTPStatus, error: str) -> None:
    _prometheus.ServerErrorCounter.labels(
        endpoint=_prometheus.endpoint(),
        status=_prometheus.status(status),
        error=error,
    ).inc()


def record_client_attempt(endpoint: str, kind: str) -> None:
    _prometheus.ClientAttemptCounter.labels(endpoint=endpoint, kind=kind).inc()


def record_retry_decision(endpoint: str, decision: str, reason: str) -> None:
    _prometheus.ClientRetryDecisionCounter.labels(
        endpoint=endpoint, decision=decision, reason=reason
    ).inc()


def record_client_error(endpoint: str, status: HTTPStatus | None, error: str) -> None:
    _prometheus.ClientErrorCounter.labels(
        endpoint=endpoint,
        status=_prometheus.status(status) if status else "unknown",
        error=error,
    ).inc()


def record_client_retries(endpoint: str, status: HTTPStatus | None, count: int) -> None:
    _prometheus.ClientRetryHistogram.labels(
        endpoint=endpoint,
        status=_prometheus.status(status) if status else "unknown",
    ).observe(count)


def record_client_response(
    endpoint: str, status: HTTPStatus | str, duration: float, size: int | None
) -> None:
    status_label = (
        _prometheus.status(status) if isinstance(status, HTTPStatus) else status
    )
    labels = {"endpoint": endpoint, "status": status_label}
    if size is not None:
        _prometheus.ClientResponseSizeHistogram.labels(**labels).observe(size)
    _prometheus.ClientLatencyHistogram.labels(**labels).observe(duration)


def record_refresh_token_invalidation(reason: str) -> None:
    _prometheus.RefreshTokenInvalidationCounter.labels(reason=reason).inc()


def record_workaround(workaround: str) -> None:
    _prometheus.WorkaroundCounter.labels(workaround=workaround).inc()
