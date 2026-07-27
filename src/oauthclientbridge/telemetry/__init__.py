from contextlib import AbstractContextManager
from http import HTTPStatus

from . import (
    _fallback,
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
    "finalize_request_metrics",
    "init_metrics",
    "init_sentry",
    "init_tracing",
    "instrument",
    "instrument_app",
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
    "request_refresh",
    "set_build_info_metric",
    "set_client_id_context",
    "set_token_state_counts_metric",
    "start_request_metrics",
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
init_tracing = _otel.init_tracing
init_metrics = _otel.init_metrics
init_sentry = _sentry.init
oauth_outcome_observer = _oauth_outcome.OAuthOutcomeObserver
fallback_observer = _fallback.FallbackObserver

otel_log_attributes = _resources.otel_log_attributes

start_request_metrics = _prometheus.start_request_metrics
finalize_request_metrics = _prometheus.finalize_request_metrics
export_metrics = _prometheus.export_metrics
observe_token_grant_age_metric = _prometheus.observe_token_grant_age_metric
set_build_info_metric = _prometheus.set_build_info_metric
set_token_state_counts_metric = _prometheus.set_token_state_counts_metric
add_refresher = _refresh.add_refresher
request_refresh = _refresh.request_refresh
start_background_refresh = _refresh.start_background_refresh
stop_background_refresh = _refresh.stop_background_refresh


def record_database_latency_metric(name: str) -> AbstractContextManager[object]:
    return _prometheus.DBLatencyHistorgram.labels(query=name).time()


def record_database_error_metric(name: str, error: str) -> None:
    _prometheus.DBErrorCounter.labels(query=name, error=error).inc()


def record_server_error_metric(
    status: HTTPStatus, error: str, endpoint: str | None = None
) -> None:
    _prometheus.ServerErrorCounter.labels(
        endpoint=endpoint or _prometheus.endpoint(),
        status=_prometheus.status(status),
        error=error,
    ).inc()


def record_client_attempt_metric(endpoint: str, kind: str) -> None:
    _prometheus.ClientAttemptCounter.labels(endpoint=endpoint, kind=kind).inc()


def record_retry_decision_metric(endpoint: str, decision: str, reason: str) -> None:
    _prometheus.ClientRetryDecisionCounter.labels(
        endpoint=endpoint, decision=decision, reason=reason
    ).inc()


def record_client_error_metric(
    endpoint: str, status: HTTPStatus | None, error: str
) -> None:
    _prometheus.ClientErrorCounter.labels(
        endpoint=endpoint,
        status=_prometheus.status(status) if status else "unknown",
        error=error,
    ).inc()


def record_client_retries_metric(
    endpoint: str, status: HTTPStatus | None, count: int
) -> None:
    _prometheus.ClientRetryHistogram.labels(
        endpoint=endpoint,
        status=_prometheus.status(status) if status else "unknown",
    ).observe(count)


def record_client_response_metric(
    endpoint: str, status: HTTPStatus | str, duration: float, size: int | None
) -> None:
    status_label = (
        _prometheus.status(status) if isinstance(status, HTTPStatus) else status
    )
    labels = {"endpoint": endpoint, "status": status_label}
    if size is not None:
        _prometheus.ClientResponseSizeHistogram.labels(**labels).observe(size)
    _prometheus.ClientLatencyHistogram.labels(**labels).observe(duration)


def record_refresh_token_invalidation_metric(reason: str) -> None:
    _prometheus.RefreshTokenInvalidationCounter.labels(reason=reason).inc()


def record_workaround_metric(workaround: str) -> None:
    _prometheus.WorkaroundCounter.labels(workaround=workaround).inc()
