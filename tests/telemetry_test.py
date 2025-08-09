import sqlite3
import unittest.mock

import flask
import pytest
import requests
import structlog
from flask.testing import FlaskClient
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics.export import HistogramDataPoint, NumberDataPoint
from requests_mock import Mocker

from oauthclientbridge import db, telemetry
from oauthclientbridge.settings import TelemetrySettings, current_settings
from oauthclientbridge.utils import APIResult

from . import otel
from .conftest import PostClient, TokenTuple

logger: structlog.BoundLogger = structlog.get_logger()

TRACE_ID = 0x1234567890ABCDEF
SPAN_ID = 0x123456
HEADER = f"00-{TRACE_ID:032x}-{SPAN_ID:016x}-01"


# WARNING: The order of fixtures matters!
# requests_mock must be before instrumented, a simple way to get this right is
# to always put the mocker first, then instrumented.


def test_telemetry_tracer_otel_enabled(
    otel_mock: otel.OTelMocker,
    tracer: trace.Tracer,
) -> None:
    """Verify that our captraces fixture works.

    I.e. we have a global trace provider wired up to this fixture.
    """

    with tracer.start_as_current_span("test-span-1"):
        with tracer.start_as_current_span("test-span-2"):
            pass

    spans = otel_mock.get_finished_spans()
    assert len(spans) == 2
    assert otel.get_span(spans, "test-span-1") is not None
    assert otel.get_span(spans, "test-span-2") is not None


def test_init_tracing_disabled() -> None:
    settings = TelemetrySettings(components=set())

    with unittest.mock.patch(
        "opentelemetry.trace.set_tracer_provider"
    ) as mock_set_tracer_provider:
        telemetry.init_tracing(settings)

    mock_set_tracer_provider.assert_not_called()


def test_telemetry_metrics_otel_enabled(
    otel_mock: otel.OTelMocker,
    meter: metrics.Meter,
) -> None:
    counter = meter.create_counter("test_counter")
    counter.add(1)

    metrics = otel_mock.get_metrics_data()
    metric = otel.get_metric(metrics, "test_counter")
    assert metric is not None
    assert metric.scope.name == "tests"
    assert metric.name == "test_counter"
    data_point = metric.metric.data.data_points[0]
    assert isinstance(data_point, NumberDataPoint)
    assert data_point.value == 1


def test_init_metrics_disabled() -> None:
    # NOTE: Avoids loop with settings.

    settings = telemetry.TelemetrySettings(components=set())

    with unittest.mock.patch(
        "opentelemetry.metrics.set_meter_provider"
    ) as mock_set_meter_provider:
        telemetry.init_metrics(settings)

    mock_set_meter_provider.assert_not_called()


def test_requests_creates_spans(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    instrumented,
    tracer: trace.Tracer,
) -> None:
    requests_mock.get("http://example.com/test")

    with tracer.start_as_current_span("test") as parent_span:
        requests.get("http://example.com/test")

    spans = otel_mock.get_finished_spans()
    requests_span = otel.get_span(spans, "GET")
    assert requests_span is not None
    otel.assert_trace_id(requests_span, parent_span)


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
    otel.assert_trace_header(
        requests_mock.request_history[0].headers["traceparent"],
        parent_span,
    )


def test_sqlite3_creates_spans(
    otel_mock: otel.OTelMocker,
    instrumented,
    tracer: trace.Tracer,
) -> None:
    with tracer.start_as_current_span("parent-span") as parent_span:
        # WARNING: `conn.execute()` is not instrumented!
        with sqlite3.connect(":memory:") as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")

    spans = otel_mock.get_finished_spans()
    assert len(spans) == 2

    select_span = otel.get_span(spans, "SELECT")
    assert select_span is not None
    assert select_span.attributes is not None
    assert select_span.attributes["db.statement"] == "SELECT 1"
    otel.assert_trace_id(select_span, parent_span)


def test_endpoint_propagates_traceparent(
    otel_mock: otel.OTelMocker,
    client: FlaskClient,
) -> None:
    client.get("/", headers={"traceparent": HEADER})

    spans = otel_mock.get_finished_spans()
    request_span = otel.get_span(spans, "GET /")
    assert request_span is not None
    otel.assert_trace_id(request_span, TRACE_ID)


def test_endpoint_sets_traceresponse_from_parent(
    otel_mock: otel.OTelMocker,
    client: FlaskClient,
    instrumented,
) -> None:
    # TODO: test without parent set?
    response = client.get("/", headers={"traceparent": HEADER})

    assert "traceresponse" in response.headers
    otel.assert_trace_header(response.headers["traceresponse"], TRACE_ID)


def test_endpoint_creates_requests_spans(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
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

    spans = otel_mock.get_finished_spans()
    outgoing_request_spans = [s for s in spans if s.name.startswith("POST")]
    assert len(outgoing_request_spans) == 1
    outgoing_request_span = outgoing_request_spans[0]
    otel.assert_trace_id(outgoing_request_span, TRACE_ID)


def test_endpoint_creates_sqlite3_spans(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
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

    spans = otel_mock.get_finished_spans()
    assert any(
        s.name.startswith("SELECT") or s.name.startswith("UPDATE") for s in spans
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
    otel.assert_trace_header(history[0].headers["traceparent"], TRACE_ID)


def test_flask_metrics(
    otel_mock: otel.OTelMocker,
    client: FlaskClient,
) -> None:
    client.get("/")

    metrics = otel_mock.get_metrics_data()
    assert otel.get_metric(metrics, "http.server.duration") is not None
    assert otel.get_metric(metrics, "http.server.active_requests") is not None


def test_requests_metrics(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    instrumented,
) -> None:
    requests_mock.get("http://example.com/test", status_code=200)

    requests.get("http://example.com/test")

    metrics = otel_mock.get_metrics_data()
    assert otel.get_metric(metrics, "http.client.duration") is not None


# NOTE: As of 2025-08-01 the sqlite3 otel auto instrumentation does not
# support metrics. So we've added our own...
def test_db_cursor_duration_metric(
    app_context: flask.ctx.AppContext,
    otel_mock: otel.OTelMocker,
):
    with db.cursor("test_operation", transaction=True) as c:
        # Perform some dummy DB operations using the provided cursor 'c'
        c.execute("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY)")
        c.execute("INSERT INTO test_table (id) VALUES (1)")

    metrics = otel_mock.get_metrics_data()
    metric = otel.get_metric(
        metrics, "oauth.db.cursor.duration", scope="oauthclientbridge.db"
    )
    assert metric is not None

    assert len(metric.metric.data.data_points) == 1
    data = metric.metric.data.data_points[0]

    assert isinstance(data, HistogramDataPoint)
    assert data.attributes is not None
    assert data.attributes["db.operation"] == "test_operation"
    assert "error.type" not in data.attributes
    assert data.count == 1


def test_db_error_metric(
    app_context: flask.ctx.AppContext,
    otel_mock: otel.OTelMocker,
):
    with pytest.raises(sqlite3.Error):
        with db.cursor("test_error_operation") as c:
            c.execute("INVALID SQL QUERY")

    metrics = otel_mock.get_metrics_data()

    # Check the error counter
    error_metric = otel.get_metric(
        metrics, "oauth.db.error.total", scope="oauthclientbridge.db"
    )
    assert error_metric is not None
    assert len(error_metric.metric.data.data_points) == 1
    error_data = error_metric.metric.data.data_points[0]
    assert isinstance(error_data, NumberDataPoint)
    assert error_data.attributes is not None
    assert error_data.attributes["db.operation"] == "test_error_operation"
    assert error_data.attributes["error.type"] == "OperationalError"
    assert error_data.value == 1

    # Check the duration histogram for the failure
    duration_metric = otel.get_metric(
        metrics, "oauth.db.cursor.duration", scope="oauthclientbridge.db"
    )
    assert duration_metric is not None
    assert len(duration_metric.metric.data.data_points) == 1
    duration_data = duration_metric.metric.data.data_points[0]
    assert isinstance(duration_data, HistogramDataPoint)
    assert duration_data.attributes is not None
    assert duration_data.attributes["db.operation"] == "test_error_operation"
    assert duration_data.attributes["error.type"] == "OperationalError"
    assert duration_data.count == 1


def test_oauth_client_metrics_success(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    post: PostClient,
    refresh_token: TokenTuple,
):
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
    )

    metrics = otel_mock.get_metrics_data()

    # TODO: Split this into two tests

    # Check duration histogram
    duration_metric = otel.get_metric(
        metrics,
        "oauth.client.duration",
        scope="oauthclientbridge.oauth",
        attributes={"operation": "refresh"},
    )
    assert duration_metric is not None
    assert len(duration_metric.metric.data.data_points) == 1
    duration_data = duration_metric.metric.data.data_points[0]
    assert isinstance(duration_data, HistogramDataPoint)
    assert duration_data.attributes is not None
    assert duration_data.attributes["operation"] == "refresh"
    assert duration_data.attributes["final.result"] == APIResult.SUCCESS
    assert "error.type" not in duration_data.attributes
    assert duration_data.count == 1

    # Check retries histogram
    retries_metric = otel.get_metric(
        metrics,
        "oauth.client.retries",
        scope="oauthclientbridge.oauth",
        attributes={"operation": "refresh"},
    )
    assert retries_metric is not None
    assert len(retries_metric.metric.data.data_points) == 1
    retries_data = retries_metric.metric.data.data_points[0]
    assert isinstance(retries_data, HistogramDataPoint)
    assert (
        retries_data.attributes == duration_data.attributes
    )  # Attributes should be identical
    assert retries_data.sum == 0  # No retries on success


def test_oauth_client_metrics_failure(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    post: PostClient,
    refresh_token: TokenTuple,
):
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"error": "invalid_grant"},
        status_code=400,
    )

    post(
        "/token",
        {
            "client_id": refresh_token.client_id,
            "client_secret": refresh_token.client_secret,
            "grant_type": "client_credentials",
        },
    )

    metrics = otel_mock.get_metrics_data()

    # Check duration histogram
    duration_metric = otel.get_metric(
        metrics,
        "oauth.client.duration",
        scope="oauthclientbridge.oauth",
        attributes={"operation": "refresh"},
    )
    assert duration_metric is not None
    assert len(duration_metric.metric.data.data_points) == 1
    duration_data = duration_metric.metric.data.data_points[0]
    assert isinstance(duration_data, HistogramDataPoint)
    assert duration_data.attributes is not None
    assert duration_data.attributes["operation"] == "refresh"
    assert duration_data.attributes["final.result"] == APIResult.CLIENT_ERROR
    assert duration_data.attributes["error.type"] == "invalid_grant"
    assert duration_data.count == 1
