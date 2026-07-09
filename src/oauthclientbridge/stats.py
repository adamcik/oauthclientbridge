import re
import time
from http import HTTPStatus
from typing import Callable

import flask
import prometheus_client
import prometheus_client.multiprocess

from oauthclientbridge.resource_labels import build_info_labels
from oauthclientbridge.settings import current_settings

registry = prometheus_client.CollectorRegistry()

TIME_BUCKETS = (
    0.0001,
    0.00055,
    0.001,
    0.0028,
    0.0046,
    0.0064,
    0.0082,
    0.01,
    0.028,
    0.046,
    0.064,
    0.082,
    0.1,
    0.4,
    0.7,
    1.0,
    4.0,
    7.0,
    10.0,
    float("inf"),
)

BYTE_BUCKETS = (
    8,
    22,
    36,
    50,
    64,
    176,
    288,
    400,
    512,
    1408,
    2304,
    3200,
    4096,
    11264,
    18432,
    25600,
    32768,
    90112,
    147456,
    204800,
    float("inf"),
)

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
    buckets=TIME_BUCKETS,
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
    buckets=TIME_BUCKETS,
    registry=registry,
)

ServerRequestSizeHistogram = prometheus_client.Histogram(
    "oauth_server_request_bytes",
    "Overall request size.",
    ["endpoint", "status"],
    buckets=BYTE_BUCKETS,
    registry=registry,
)

ServerResponseSizeHistogram = prometheus_client.Histogram(
    "oauth_server_response_bytes",
    "Overall response size.",
    ["endpoint", "status"],
    buckets=BYTE_BUCKETS,
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
    buckets=TIME_BUCKETS,
    registry=registry,
)

ClientResponseSizeHistogram = prometheus_client.Histogram(
    "oauth_client_response_bytes",
    "Overall response size.",
    ["endpoint", "status"],
    buckets=BYTE_BUCKETS,
    registry=registry,
)

BuildInfoGauge = prometheus_client.Gauge(
    "oauth_build_info",
    "Build and deployment metadata.",
    [
        "service_name",
        "service_namespace",
        "service_instance_id",
        "deployment_environment",
        "oauth_provider",
        "service_version",
        "vcs_revision",
    ],
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

TokenStateGauge = prometheus_client.Gauge(
    "oauth_token_records",
    "Stored token records by database state.",
    ["state"],
    multiprocess_mode="mostrecent",
    registry=registry,
)

_build_info_values: tuple[str, str, str, str, str, str, str] | None = None
_token_state_counts_initialized = False


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
    if response.content_length is not None:
        ServerResponseSizeHistogram.labels(**labels).observe(response.content_length)
    if flask.request.content_length is not None:
        ServerRequestSizeHistogram.labels(**labels).observe(
            flask.request.content_length
        )
    return response


def export_metrics() -> flask.Response:
    metrics_registry = registry
    multiproc_dir = current_settings.prometheus.multiproc_dir
    if multiproc_dir:
        metrics_registry = prometheus_client.CollectorRegistry()
        prometheus_client.multiprocess.MultiProcessCollector(
            metrics_registry,
            path=str(multiproc_dir),
        )

    text = prometheus_client.generate_latest(metrics_registry)
    return flask.Response(text, mimetype=prometheus_client.CONTENT_TYPE_LATEST)


def set_build_info(settings) -> None:
    global _build_info_values

    labels_dict = build_info_labels(settings)
    labels = (
        labels_dict["service_name"],
        labels_dict["service_namespace"],
        labels_dict["service_instance_id"],
        labels_dict["deployment_environment"],
        labels_dict["oauth_provider"],
        labels_dict["service_version"],
        labels_dict["vcs_revision"],
    )
    if _build_info_values == labels:
        return

    BuildInfoGauge.labels(**labels_dict).set(1)
    _build_info_values = labels


def set_token_state_counts(counts: dict[str, int]) -> None:
    global _token_state_counts_initialized

    for state, count in counts.items():
        TokenStateGauge.labels(state=state).set(count)

    _token_state_counts_initialized = True


def ensure_token_state_counts(counts: Callable[[], dict[str, int]]) -> None:
    if _token_state_counts_initialized:
        return

    set_token_state_counts(counts())
