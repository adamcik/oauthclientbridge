import sqlite3
import unittest.mock
from typing import assert_never

import flask
import pytest
import requests
import structlog
from flask.testing import FlaskClient
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics.export import HistogramDataPoint
from opentelemetry.sdk.trace import ReadableSpan
from requests_mock import Mocker

from oauthclientbridge import telemetry
from oauthclientbridge.settings import (
    TelemetryComponent,
    TelemetrySettings,
    current_settings,
)

from .conftest import PostClient, TokenTuple
from .otel_mocker import OTelMocker

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


@pytest.fixture(autouse=True)
def init_telemetry(otel_mock: OTelMocker) -> None:
    settings = TelemetrySettings(
        components={
            TelemetryComponent.TRACING,
            TelemetryComponent.METRICS,
        }
    )
    telemetry.init_metrics(settings, otel_mock.metric_reader)
    telemetry.init_tracing(settings, otel_mock.span_processor)


def test_telemetry_tracer_otel_enabled(
    otel_mock: OTelMocker,
    tracer: trace.Tracer,
) -> None:
    """Verify that our captraces fixture works.

    I.e. we have a global trace provider wired up to this fixture.
    """

    with tracer.start_as_current_span("test-span-1"):
        with tracer.start_as_current_span("test-span-2"):
            pass

    print(otel_mock._span_exporter.get_finished_spans())

    assert len(otel_mock.get_finished_spans()) == 2
    otel_mock.assert_has_span_named("test-span-1")
    otel_mock.assert_has_span_named("test-span-2")


def test_init_tracing_disabled() -> None:
    # NOTE: Avoids loop with settings.
    from oauthclientbridge import telemetry

    settings = telemetry.TelemetrySettings(components=set())

    with unittest.mock.patch(
        "opentelemetry.trace.set_tracer_provider"
    ) as mock_set_tracer_provider:
        telemetry.init_tracing(settings)

    mock_set_tracer_provider.assert_not_called()


def test_telemetry_metrics_otel_enabled(
    otel_mock: OTelMocker,
    meter: metrics.Meter,
) -> None:
    counter = meter.create_counter("test_counter")
    counter.add(1)

    metrics_data = otel_mock.get_metrics_data_named("test_counter")[0]
    assert metrics_data.scope.name == "tests"
    assert metrics_data.metric.name == "test_counter"
    assert metrics_data.metric.data.data_points[0].value == 1


def test_init_metrics_disabled() -> None:
    # NOTE: Avoids loop with settings.
    from oauthclientbridge import telemetry

    settings = telemetry.TelemetrySettings(components=set())

    with unittest.mock.patch(
        "opentelemetry.metrics.set_meter_provider"
    ) as mock_set_meter_provider:
        telemetry.init_metrics(settings)

    mock_set_meter_provider.assert_not_called()


def test_requests_creates_spans(
    requests_mock: Mocker,
    otel_mock: OTelMocker,
    instrumented,
    tracer: trace.Tracer,
) -> None:
    requests_mock.get("http://example.com/test")

    with tracer.start_as_current_span("test") as parent_span:
        requests.get("http://example.com/test")

    requests_span = otel_mock.get_span_named("GET")
    assert requests_span is not None
    assert_trace_id(parent_span, requests_span)


def test_requests_propagates_header(
    requests_mock: Mocker,
    instrumented,
    tracer: trace.Tracer,
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
    otel_mock: OTelMocker,
    instrumented,
    tracer: trace.Tracer,
) -> None:
    with tracer.start_as_current_span("parent-span") as parent_span:
        # WARNING: `conn.execute()` is not instrumented!
        with sqlite3.connect(":memory:") as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")

    finished_spans = otel_mock.get_finished_spans()
    assert len(finished_spans) == 2

    select_span = otel_mock.get_span_named("SELECT")
    assert select_span is not None
    assert select_span.attributes is not None
    assert select_span.attributes["db.statement"] == "SELECT 1"
    assert_trace_id(parent_span, select_span)


def test_endpoint_propagates_traceparent(
    otel_mock: OTelMocker,
    client: FlaskClient,
) -> None:
    client.get("/", headers={"traceparent": HEADER})

    request_span = otel_mock.get_span_named("GET /")
    assert request_span is not None
    assert_trace_id(TRACE_ID, request_span)


def test_endpoint_sets_traceresponse_from_parent(
    otel_mock: OTelMocker,
    client: FlaskClient,
    instrumented,
) -> None:
    # TODO: test without parent set?
    response = client.get("/", headers={"traceparent": HEADER})

    # FIXME: This works outside of tests now
    assert "traceresponse" in response.headers
    assert_trace_header(TRACE_ID, response.headers["traceresponse"])


def test_endpoint_creates_requests_spans(
    requests_mock: Mocker,
    otel_mock: OTelMocker,
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

    finished_spans = otel_mock.get_finished_spans()
    outgoing_request_span = next(s for s in finished_spans if s.name.startswith("POST"))
    assert_trace_id(TRACE_ID, outgoing_request_span)


def test_endpoint_creates_sqlite3_spans(
    requests_mock: Mocker,
    otel_mock: OTelMocker,
    instrumented,
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
        for s in otel_mock.get_finished_spans()
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


def test_flask_metrics(
    otel_mock: OTelMocker,
    client: FlaskClient,
) -> None:
    client.get("/")

    otel_mock.assert_has_metrics_data_named("http.server.duration")
    otel_mock.assert_has_metrics_data_named("http.server.active_requests")


def test_requests_metrics(
    requests_mock: Mocker,
    otel_mock: OTelMocker,
    instrumented,
) -> None:
    requests_mock.get("http://example.com/test", status_code=200)

    requests.get("http://example.com/test")

    otel_mock.assert_has_metrics_data_named("http.client.duration")


# NOTE: As of 2025-08-01 the sqlite3 otel auto instrumentation does not
# support metrics. So we've added our own...
def test_db_cursor_duration_metric(
    app_context: flask.ctx.AppContext,
    otel_mock: OTelMocker,
):
    telemetry.init_metrics(
        TelemetrySettings(components={TelemetryComponent.METRICS}),
        otel_mock.metric_reader,
    )

    with db.cursor("test_operation", transaction=True) as c:
        # Perform some dummy DB operations using the provided cursor 'c'
        c.execute("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY)")
        c.execute("INSERT INTO test_table (id) VALUES (1)")

    metrics_data = otel_mock.get_metrics_data_named("oauth.db.cursor.duration")
    assert len(metrics_data) > 0
    assert metrics_data[0].scope.name == "oauthclientbridge.db"

    assert len(metrics_data[0].metric.data.data_points) == 1
    data = metrics_data[0].metric.data.data_points[0]

    assert isinstance(data, HistogramDataPoint)
    assert data.attributes is not None
    assert data.attributes["oauth.db.cursor.name"] == "test_operation"
    assert data.attributes["oauth.db.cursor.transaction"] is True
    assert data.count == 1
