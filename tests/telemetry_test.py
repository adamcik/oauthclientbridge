import sqlite3
from typing import assert_never, cast

import requests
import structlog
from flask.testing import FlaskClient
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricsData,
    NumberDataPoint,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from requests_mock import Mocker

from oauthclientbridge.settings import current_settings

from .conftest import PostClient, TokenTuple

tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

logger: structlog.BoundLogger = structlog.get_logger()

TRACE_ID = 0x1234567890ABCDEF
SPAN_ID = 0x123456
HEADER = f"00-{TRACE_ID:032x}-{SPAN_ID:016x}-01"


# WARNING: The order of fixtures matters!
# requests_mock must be before instrumented, a simple way to get this right is
# to always put the mocker first, then instrumented.


def assert_trace_id(expected: int | trace.Span, readable_span: ReadableSpan):
    extepected_trace_id = _extract_trace_id(expected)
    readable_context = readable_span.get_span_context()
    assert readable_context is not None
    assert extepected_trace_id == readable_context.trace_id


def assert_trace_header(expected: int | trace.Span, header: str):
    expected_header = f"00-{_extract_trace_id(expected):032x}-"
    assert header.startswith(expected_header)


def _extract_trace_id(expected: trace.Span | int) -> int:
    match expected:
        case int():
            return expected
        case trace.Span():
            return expected.get_span_context().trace_id
        case _:
            assert_never(expected)


def test_telemetry_tracer_otel_enabled(
    captraces: InMemorySpanExporter,
) -> None:
    """Verify that our captraces fixture works.

    I.e. we have a global trace provider wired up to this fixture.
    """

    with tracer.start_as_current_span("test-span-1"):
        with tracer.start_as_current_span("test-span-2"):
            pass

    finished_spans = captraces.get_finished_spans()
    assert len(finished_spans) == 2
    assert "test-span-1" in [span.name for span in finished_spans]
    assert "test-span-2" in [span.name for span in finished_spans]


def test_telemetry_metrics_otel_enabled(
    capmetrics: InMemoryMetricReader,
) -> None:
    """Verify that our capmetrics fixture works.

    I.e. we have a global metric provider wired up to this fixture.
    """

    counter = meter.create_counter("test_counter")
    counter.add(1)

    metrics_data = capmetrics.get_metrics_data()
    logger.debug("Collected metrics data", metrics_data=metrics_data)
    assert metrics_data is not None

    metrics_data = cast(MetricsData, metrics_data)
    scope_metrics = metrics_data.resource_metrics[0].scope_metrics

    test_scope = next(
        s for s in scope_metrics if s.scope.name == "tests.telemetry_test"
    )
    assert len(test_scope.metrics) == 1

    metric = test_scope.metrics[0]
    assert metric.name == "test_counter"

    data_point = metric.data.data_points[0]
    assert isinstance(data_point, NumberDataPoint)
    assert data_point.value == 1


def test_requests_creates_spans(
    requests_mock: Mocker,
    instrumented,
    captraces: InMemorySpanExporter,
) -> None:
    requests_mock.get("http://example.com/test")

    with tracer.start_as_current_span("test") as parent_span:
        requests.get("http://example.com/test")

    assert_trace_id(
        parent_span,
        next(s for s in captraces.get_finished_spans() if s.name.startswith("GET")),
    )


def test_requests_propagates_header(
    requests_mock: Mocker,
    instrumented,
) -> None:
    requests_mock.get("http://example.com/test", status_code=200, json={})

    with tracer.start_as_current_span("parent-span") as parent_span:
        requests.get("http://example.com/test")

    assert len(requests_mock.request_history) == 1
    assert "traceparent" in requests_mock.request_history[0].headers
    assert_trace_header(
        parent_span,
        requests_mock.request_history[0].headers["traceparent"],
    )


def test_sqlite3_creates_spans(
    instrumented,
    captraces: InMemorySpanExporter,
) -> None:
    with tracer.start_as_current_span("parent-span") as parent_span:
        # WARNING: `conn.execute()` is not instrumented!
        with sqlite3.connect(":memory:") as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")

    finished_spans = captraces.get_finished_spans()
    assert len(finished_spans) == 2

    select_span = next(s for s in finished_spans if s.name == "SELECT")
    assert select_span.attributes is not None
    assert select_span.attributes["db.statement"] == "SELECT 1"
    assert_trace_id(parent_span, select_span)


def test_endpoint_propagates_traceparent(
    captraces: InMemorySpanExporter,
    client: FlaskClient,
) -> None:
    client.get("/", headers={"traceparent": HEADER})

    finished_spans = captraces.get_finished_spans()
    assert len(finished_spans) >= 1

    request_span = next(s for s in finished_spans if s.name == "GET /")
    assert_trace_id(TRACE_ID, request_span)


def test_endpoint_sets_traceresponse(
    client: FlaskClient,
) -> None:
    response = client.get("/", headers={"traceparent": HEADER})

    assert "traceresponse" in response.headers
    assert_trace_header(TRACE_ID, response.headers["traceresponse"])


def test_endpoint_creates_requests_spans(
    requests_mock: Mocker,
    captraces: InMemorySpanExporter,
    post: PostClient,
    refresh_token: TokenTuple,
) -> None:
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"access_token": "mock_token", "token_type": "Bearer"},
        status_code=200,
    )

    post(
        "/token",
        {
            "client_id": refresh_token.client_id,
            "client_secret": refresh_token.client_secret,
            "grant_type": "client_credentials",
        },
        headers={"traceparent": HEADER},
    )

    finished_spans = captraces.get_finished_spans()
    outgoing_request_span = next(s for s in finished_spans if s.name.startswith("POST"))
    assert_trace_id(TRACE_ID, outgoing_request_span)


def test_endpoint_creates_sqlite3_spans(
    requests_mock: Mocker,
    instrumented,
    captraces: InMemorySpanExporter,
    post: PostClient,
    refresh_token: TokenTuple,
) -> None:
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"access_token": "mock_token", "token_type": "Bearer"},
        status_code=200,
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    post("/token", data)

    assert any(
        s.name.startswith("SELECT") or s.name.startswith("UPDATE")
        for s in captraces.get_finished_spans()
    )


def test_endpoint_propagates_outgoing_traceparent(
    requests_mock: Mocker,
    instrumented,
    post: PostClient,
    refresh_token: TokenTuple,
) -> None:
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"access_token": "mock_token", "token_type": "Bearer"},
        status_code=200,
    )

    post(
        "/token",
        {
            "client_id": refresh_token.client_id,
            "client_secret": refresh_token.client_secret,
            "grant_type": "client_credentials",
        },
        headers={"traceparent": HEADER},
    )

    history = requests_mock.request_history
    assert len(history) == 1
    assert "traceparent" in history[0].headers
    assert_trace_header(TRACE_ID, history[0].headers["traceparent"])


def test_flask_metrics(client: FlaskClient, capmetrics: InMemoryMetricReader) -> None:
    client.get("/")

    metrics_data = capmetrics.get_metrics_data()
    assert metrics_data is not None

    scope_metrics = metrics_data.resource_metrics[0].scope_metrics

    flask_scope = next(
        s
        for s in scope_metrics
        if s.scope.name == "opentelemetry.instrumentation.flask"
    )

    assert any(m.name == "http.server.duration" for m in flask_scope.metrics)
    assert any(m.name == "http.server.active_requests" for m in flask_scope.metrics)


def test_requests_metrics(
    requests_mock: Mocker,
    instrumented,
    capmetrics: InMemoryMetricReader,
) -> None:
    requests_mock.get("http://example.com/test", status_code=200)

    requests.get("http://example.com/test")

    metrics_data = capmetrics.get_metrics_data()
    assert metrics_data is not None

    scope_metrics = metrics_data.resource_metrics[0].scope_metrics

    requests_scope = next(
        s
        for s in scope_metrics
        if s.scope.name == "opentelemetry.instrumentation.requests"
    )

    assert any(m.name == "http.client.duration" for m in requests_scope.metrics)


# NOTE: As of 2025-08-01 the sqlite3 otel instrumentation does not support metrics.
