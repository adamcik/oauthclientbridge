import re
import time
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

import flask
import prometheus_client
import prometheus_client.multiprocess

from oauthclientbridge.settings import TelemetrySettings, current_settings
from oauthclientbridge.utils import time as time_utils

from ._buckets import BYTES, TIME, TOKEN_GRANT_AGE
from ._resources import BuildInfoLabels, build_info_labels

registry = prometheus_client.CollectorRegistry()
RETRY_BUCKETS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, float("inf"))

HTTP_STATUS_LABELS: dict[HTTPStatus, str] = {}

DBErrorCounter = prometheus_client.Counter(
    "oauth_database_error_total",
    "Database errors.",
    ["query", "error"],
    registry=registry,
)

DBLatencyHistorgram = prometheus_client.Histogram(
    "oauth_database_latency_seconds",
    "Database query latency.",
    ["query"],
    buckets=TIME,
    registry=registry,
)

ServerErrorCounter = prometheus_client.Counter(
    "oauth_server_error_total",
    "OAuth errors returned to users.",
    ["endpoint", "status", "error"],
    registry=registry,
)

ServerLatencyHistogram = prometheus_client.Histogram(
    "oauth_server_latency_seconds",
    "Overall request latency.",
    ["endpoint", "status"],
    buckets=TIME,
    registry=registry,
)

ServerRequestSizeHistogram = prometheus_client.Histogram(
    "oauth_server_request_bytes",
    "Overall request size.",
    ["endpoint", "status"],
    buckets=BYTES,
    registry=registry,
)

ServerResponseSizeHistogram = prometheus_client.Histogram(
    "oauth_server_response_bytes",
    "Overall response size.",
    ["endpoint", "status"],
    buckets=BYTES,
    registry=registry,
)

ClientErrorCounter = prometheus_client.Counter(
    "oauth_client_error_total",
    "OAuth errors from upstream provider.",
    ["endpoint", "status", "error"],
    registry=registry,
)

ClientRetryHistogram = prometheus_client.Histogram(
    "oauth_client_retries",
    "OAuth fetch retries.",
    ["endpoint", "status"],
    buckets=RETRY_BUCKETS,
    registry=registry,
)

ClientAttemptCounter = prometheus_client.Counter(
    "oauth_client_attempts",
    "OAuth client attempts, including retries.",
    ["endpoint", "kind"],
    registry=registry,
)

ClientRetryDecisionCounter = prometheus_client.Counter(
    "oauth_client_retry_decisions",
    "OAuth retry decisions and reasons.",
    ["endpoint", "decision", "reason"],
    registry=registry,
)

ClientLatencyHistogram = prometheus_client.Histogram(
    "oauth_client_latency_seconds",
    "Overall request latency.",
    ["endpoint", "status"],
    buckets=TIME,
    registry=registry,
)

ClientResponseSizeHistogram = prometheus_client.Histogram(
    "oauth_client_response_bytes",
    "Overall response size.",
    ["endpoint", "status"],
    buckets=BYTES,
    registry=registry,
)


BuildInfoGauge = prometheus_client.Gauge(
    "oauth_build_info",
    "Build and deployment metadata.",
    BuildInfoLabels.__annotations__.keys(),
    multiprocess_mode="max",
    registry=registry,
)

RefreshTokenInvalidationCounter = prometheus_client.Counter(
    "oauth_refresh_token_invalidations_total",
    "Stored refresh tokens invalidated locally after authoritative upstream failures.",
    ["reason"],
    registry=registry,
)

WorkaroundCounter = prometheus_client.Counter(
    "oauth_workarounds_total",
    "Temporary workaround activations.",
    ["workaround"],
    registry=registry,
)

TokenGrantAgeHistogram = prometheus_client.Histogram(
    "oauth_token_grant_age_seconds",
    "Age of successfully used stored token grants.",
    buckets=TOKEN_GRANT_AGE,
    registry=registry,
)

TokenStateGauge = prometheus_client.Gauge(
    "oauth_token_records",
    "Stored token records by database state.",
    ["state"],
    multiprocess_mode="mostrecent",
    registry=registry,
)

_multiprocess_registries: dict[Path, prometheus_client.CollectorRegistry] = {}


def status(code: HTTPStatus) -> str:
    if code not in HTTP_STATUS_LABELS:
        phrase = re.sub(r"[ -]", "_", code.name.lower())
        HTTP_STATUS_LABELS[code] = f"http_{phrase}"
    return HTTP_STATUS_LABELS[code]


def endpoint() -> str:
    return getattr(flask.request.url_rule, "endpoint", "notfound")


def record_metrics() -> None:
    flask.g.stats_latency_start_time = time.time()


def finalize_metrics(response: flask.Response) -> flask.Response:
    request_latency = time.time() - flask.g.stats_latency_start_time
    labels = {
        "endpoint": endpoint(),
        "status": status(HTTPStatus(response.status_code)),
    }

    ServerLatencyHistogram.labels(**labels).observe(request_latency)
    response_content_length = response.headers.get("Content-Length")
    if response_content_length is not None:
        ServerResponseSizeHistogram.labels(**labels).observe(
            int(response_content_length)
        )
    if flask.request.content_length is not None:
        ServerRequestSizeHistogram.labels(**labels).observe(
            flask.request.content_length
        )
    return response


def export_metrics() -> flask.Response:
    metrics_registry = registry
    multiproc_dir = current_settings.prometheus.multiproc_dir
    if multiproc_dir:
        metrics_registry = _multiprocess_registries.get(multiproc_dir)
        if metrics_registry is None:
            metrics_registry = prometheus_client.CollectorRegistry()
            prometheus_client.multiprocess.MultiProcessCollector(
                metrics_registry,
                path=str(multiproc_dir),
            )
            _multiprocess_registries[multiproc_dir] = metrics_registry

    text = prometheus_client.generate_latest(metrics_registry)
    return flask.Response(text, mimetype=prometheus_client.CONTENT_TYPE_LATEST)


def observe_token_grant_age(created_at: datetime | None) -> None:
    if created_at is None:
        return

    TokenGrantAgeHistogram.observe((time_utils.utcnow() - created_at).total_seconds())


def set_build_info(settings: TelemetrySettings) -> None:
    BuildInfoGauge.labels(**build_info_labels(settings)).set(1)


def set_token_state_counts(counts: dict[str, int]) -> None:
    for state, count in counts.items():
        TokenStateGauge.labels(state=state).set(count)
