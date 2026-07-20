import os
import sqlite3
import sys
import unittest.mock
from http import HTTPStatus

import flask
import pytest
import requests
import structlog
from flask.testing import FlaskClient
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics.export import HistogramDataPoint, NumberDataPoint
from requests_mock import Mocker

from oauthclientbridge import db, oauth, telemetry
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.oauth import (
    _core as oauth_core,  # pyright: ignore[reportPrivateUsage] # Direct implementation test.
)
from oauthclientbridge.oauth._outcome import (  # pyright: ignore[reportPrivateUsage] # Direct implementation test.
    OAuthResponse,
    UpstreamResult,
)
from oauthclientbridge.settings import (
    Settings,
    TelemetryComponent,
    TelemetrySettings,
    current_settings,
)
from oauthclientbridge.telemetry import _resources as resource_labels

from .conftest import GetClient, PostClient, TokenTuple
from .plugins import otel

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

    settings = TelemetrySettings(components=set())

    with unittest.mock.patch(
        "oauthclientbridge.telemetry._otel.set_meter_provider"
    ) as mock_set_meter_provider:
        telemetry.init_metrics(settings)

    mock_set_meter_provider.assert_not_called()


def test_init_metrics_sets_resource_attributes() -> None:
    settings = TelemetrySettings(
        components={TelemetryComponent.METRICS},
        service_name="test-service",
        service_namespace="oauthclientbridge",
        service_version="1.2.3",
        deployment_environment="testing",
        oauth_provider="spotify",
        service_instance_id="delta-testing-spotify",
        vcs_revision="abc1234",
    )

    with unittest.mock.patch(
        "oauthclientbridge.telemetry._otel.set_meter_provider"
    ) as mock_set_meter_provider:
        telemetry.init_metrics(settings)

    provider = mock_set_meter_provider.call_args.args[0]
    attrs = provider._sdk_config.resource.attributes
    assert attrs["service.name"] == "test-service"
    assert attrs["service.namespace"] == "oauthclientbridge"
    assert attrs["service.version"] == "1.2.3"
    assert attrs["deployment.environment"] == "testing"
    assert attrs["oauth.provider"] == "spotify"
    assert attrs["service.instance.id"] == "delta-testing-spotify"
    assert attrs["vcs.revision"] == "abc1234"
    assert attrs["process.pid"] == os.getpid()


def test_init_metrics_derives_default_instance_id_outside_uwsgi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_labels.socket, "gethostname", lambda: "delta")
    monkeypatch.setattr(resource_labels, "_uwsgi_worker_id", lambda: None)
    settings = TelemetrySettings(
        components={TelemetryComponent.METRICS},
        deployment_environment="testing",
        oauth_provider="spotify",
    )

    with unittest.mock.patch(
        "oauthclientbridge.telemetry._otel.set_meter_provider"
    ) as mock_set_meter_provider:
        telemetry.init_metrics(settings)

    provider = mock_set_meter_provider.call_args.args[0]
    attrs = provider._sdk_config.resource.attributes
    assert attrs["service.instance.id"] == "delta-spotify-testing"


def test_init_metrics_uses_uwsgi_worker_id_for_default_instance_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_labels.socket, "gethostname", lambda: "delta")
    monkeypatch.setitem(
        sys.modules,
        "uwsgi",
        unittest.mock.Mock(worker_id=unittest.mock.Mock(return_value=3)),
    )
    settings = TelemetrySettings(
        components={TelemetryComponent.METRICS},
        deployment_environment="testing",
        oauth_provider="spotify",
    )

    with unittest.mock.patch(
        "oauthclientbridge.telemetry._otel.set_meter_provider"
    ) as mock_set_meter_provider:
        telemetry.init_metrics(settings)

    provider = mock_set_meter_provider.call_args.args[0]
    attrs = provider._sdk_config.resource.attributes
    assert attrs["service.instance.id"] == "delta-spotify-testing-3"


def test_log_attributes_preserves_numeric_process_id() -> None:
    assert resource_labels.log_attributes(
        {
            "service.name": "test-service",
            "process.pid": 1234,
        }
    ) == {
        "service.name": "test-service",
        "process.pid": 1234,
    }


def test_requests_creates_spans(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    instrumented: None,
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
    instrumented: None,
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
    instrumented: None,
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


def test_local_invalid_grant_records_handled_trace_error(
    otel_mock: otel.OTelMocker,
    post: PostClient,
    access_token: TokenTuple,
) -> None:
    _ = db.update(access_token.client_id, None)

    response = post(
        "/token",
        {
            "client_id": access_token.client_id,
            "client_secret": access_token.client_secret,
            "grant_type": "client_credentials",
        },
    )

    assert response.status == 400
    assert response.data["error"] == OAuthError.INVALID_GRANT

    spans = otel_mock.get_finished_spans()
    request_span = otel.get_span(spans, "POST /token")
    assert request_span is not None
    assert request_span.attributes is not None
    assert request_span.attributes["client_id"] == str(access_token.client_id)
    assert request_span.attributes["error.unhandled"] is False
    assert request_span.attributes["oauth.error"] == "invalid_grant"
    assert request_span.status.status_code == trace.StatusCode.ERROR
    assert any(event.name == "exception" for event in request_span.events)


def test_malformed_client_id_records_rejected_value(
    otel_mock: otel.OTelMocker,
    post: PostClient,
) -> None:
    malformed_client_id = "# SPOTIFY_CLIENT_ID"

    response = post(
        "/token",
        {
            "client_id": malformed_client_id,
            "client_secret": "secret",
            "grant_type": "client_credentials",
        },
    )

    assert response.status == HTTPStatus.UNAUTHORIZED

    spans = otel_mock.get_finished_spans()
    request_span = otel.get_span(spans, "POST /token")
    assert request_span is not None
    invalid_client_id_event = next(
        event for event in request_span.events if event.name == "invalid_client_id"
    )
    event_attributes = invalid_client_id_event.attributes
    assert event_attributes is not None
    assert event_attributes["client_id"] == malformed_client_id


def test_callback_missing_state_records_trace_error_message(
    otel_mock: otel.OTelMocker,
    get: GetClient,
) -> None:
    response = get("/callback?code=1234")

    assert response.status == 400
    assert response.data["error"] == OAuthError.INVALID_STATE

    spans = otel_mock.get_finished_spans()
    request_span = otel.get_span(spans, "GET /callback")
    assert request_span is not None

    error_event = next(event for event in request_span.events if event.name == "error")
    event_attributes = error_event.attributes
    assert event_attributes is not None
    error_message = event_attributes["exception.message"]
    assert isinstance(error_message, str)
    assert error_message.startswith("invalid_state:")


def test_callback_trace_redacts_query_values(
    otel_mock: otel.OTelMocker,
    get: GetClient,
) -> None:
    get("/callback?code=secret-code&state=secret-state")

    spans = otel_mock.get_finished_spans()
    request_span = otel.get_span(spans, "GET /callback")
    assert request_span is not None
    assert request_span.attributes is not None
    assert request_span.attributes["http.url"] == (
        "http://localhost/callback?code=<REDACTED>&state=<REDACTED>"
    )
    assert request_span.attributes["url.full"] == (
        "http://localhost/callback?code=<REDACTED>&state=<REDACTED>"
    )
    assert request_span.attributes["url.query"] == "code=<REDACTED>&state=<REDACTED>"


def test_authorize_trace_redacts_redirect_location(
    otel_mock: otel.OTelMocker,
    client: FlaskClient,
) -> None:
    response = client.get("/")

    assert response.status_code == HTTPStatus.FOUND
    spans = otel_mock.get_finished_spans()
    request_span = otel.get_span(spans, "GET /")
    assert request_span is not None
    assert request_span.attributes is not None
    location = request_span.attributes["http.response.header.location"]
    assert isinstance(location, str)
    assert location.startswith("https://provider.example.com/auth?")
    assert "state=<REDACTED>" in location
    assert "client_secret" not in location


def test_unhandled_server_error_marks_span_unhandled(
    otel_mock: otel.OTelMocker,
    client: FlaskClient,
) -> None:
    app = client.application

    def boom() -> str:
        raise RuntimeError("boom")

    app.add_url_rule("/boom", view_func=boom)

    response = client.get("/boom")

    assert response.status_code == 500

    spans = otel_mock.get_finished_spans()
    request_span = otel.get_span(spans, "GET /boom")
    assert request_span is not None
    assert request_span.attributes is not None
    assert request_span.attributes["error.unhandled"] is True
    assert request_span.status.status_code == trace.StatusCode.ERROR
    assert any(event.name == "exception" for event in request_span.events)


def test_endpoint_sets_traceresponse_from_parent(
    otel_mock: otel.OTelMocker,
    client: FlaskClient,
    instrumented: None,
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


def test_outgoing_request_span_records_retry_after_header(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    app_context: flask.ctx.AppContext,
    instrumented: None,
) -> None:
    current_settings.fetch.total_retries = 1
    requests_mock.post(
        current_settings.oauth.token_uri,
        status_code=429,
        headers={"Content-Type": "application/json", "Retry-After": "10"},
        json={"error": "temporarily_unavailable"},
    )

    oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    spans = otel_mock.get_finished_spans()
    outgoing_request_spans = otel.find_spans(spans, "POST")
    assert len(outgoing_request_spans) == 2
    for outgoing_request_span in outgoing_request_spans:
        assert outgoing_request_span.attributes is not None
        assert outgoing_request_span.attributes[
            "http.response.header.content_type"
        ] == ("application/json")
        assert (
            outgoing_request_span.attributes["http.response.header.retry_after"] == "10"
        )


def test_endpoint_creates_sqlite3_spans(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    instrumented: None,
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


def test_token_update_records_changed_fields(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    instrumented: None,
    post: PostClient,
    refresh_token: TokenTuple,
) -> None:
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={
            "access_token": "mock_token",
            "token_type": "Bearer",
            "refresh_token": "rotated_refresh_token",
        },
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

    spans = otel_mock.get_finished_spans()
    request_span = otel.get_span(spans, "POST /token")
    assert request_span is not None
    events = [event for event in request_span.events if event.name == "Updating token"]

    assert len(events) == 1
    assert events[0].attributes == {"updated_fields": ("refresh_token",)}


def test_token_insert_records_fields(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    instrumented: None,
    get: GetClient,
    state: str,
) -> None:
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={
            "access_token": "mock_token",
            "refresh_token": "refresh_token",
            "token_type": "Bearer",
        },
        status_code=200,
    )

    get("/callback?code=1234&state=" + state)

    spans = otel_mock.get_finished_spans()
    request_span = otel.get_span(spans, "GET /callback")
    assert request_span is not None
    events = [event for event in request_span.events if event.name == "Inserting token"]

    assert len(events) == 1
    assert events[0].attributes == {"inserted_fields": ("refresh_token",)}


def test_revoked_grant_workaround_adds_span_event(
    otel_mock: otel.OTelMocker,
    post: PostClient,
    access_token: TokenTuple,
    settings: Settings,
) -> None:
    settings.revoked_grant_workaround_user_agents = r"^Mopidy-Spotify/4\.1\.1\b"

    _ = db.update(access_token.client_id, None)

    post(
        "/token",
        {
            "client_id": access_token.client_id,
            "client_secret": access_token.client_secret,
            "grant_type": "client_credentials",
        },
        headers={"User-Agent": "Mopidy-Spotify/4.1.1 Mopidy/3.4.2 CPython/3.11.2"},
    )

    spans = otel_mock.get_finished_spans()
    request_span = next(s for s in spans if s.name == "POST /token")

    assert any(
        event.name == "Served revoked grant workaround token"
        for event in request_span.events
    )


def test_endpoint_propagates_outgoing_traceparent(
    requests_mock: Mocker,
    instrumented: None,
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
    instrumented: None,
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
    data = otel.latest_metric_data(
        metrics,
        "oauth.db.cursor.duration",
        HistogramDataPoint,
        attributes={"db.operation": "test_operation"},
        scope="oauthclientbridge.db",
    )

    assert data.attributes is not None
    assert data.attributes["db.operation"] == "test_operation"
    assert "error.type" not in data.attributes
    assert data.count == 1


def test_db_cursor_count_metric_success(
    app_context: flask.ctx.AppContext,
    otel_mock: otel.OTelMocker,
):
    with db.cursor("test_operation", transaction=True) as c:
        c.execute("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY)")
        c.execute("INSERT INTO test_table (id) VALUES (1)")

    metrics = otel_mock.get_metrics_data()
    data = otel.latest_metric_data(
        metrics,
        "oauth.db.cursor.total",
        NumberDataPoint,
        attributes={"db.operation": "test_operation"},
        scope="oauthclientbridge.db",
    )

    assert data.attributes is not None
    assert data.attributes["db.operation"] == "test_operation"
    assert data.attributes["transaction"] is True
    assert data.attributes["db.system"] == "sqlite"
    assert data.attributes["db.name"] == current_settings.database.database
    assert "error.type" not in data.attributes
    assert data.value == 1


def test_db_cursor_duration_metric_error(
    app_context: flask.ctx.AppContext,
    otel_mock: otel.OTelMocker,
):
    with pytest.raises(sqlite3.Error):
        with db.cursor("test_error_operation") as c:
            c.execute("INVALID SQL QUERY")

    metrics = otel_mock.get_metrics_data()
    data = otel.latest_metric_data(
        metrics,
        "oauth.db.cursor.duration",
        HistogramDataPoint,
        attributes={"db.operation": "test_error_operation"},
        scope="oauthclientbridge.db",
    )

    assert data.attributes is not None
    assert data.attributes["db.operation"] == "test_error_operation"
    assert data.attributes["error.type"] == "OperationalError"
    assert data.count == 1


def test_db_cursor_count_metric_error(
    app_context: flask.ctx.AppContext,
    otel_mock: otel.OTelMocker,
):
    with pytest.raises(sqlite3.Error):
        with db.cursor("test_error_operation") as c:
            c.execute("INVALID SQL QUERY")

    metrics = otel_mock.get_metrics_data()
    data = otel.latest_metric_data(
        metrics,
        "oauth.db.cursor.total",
        NumberDataPoint,
        attributes={"db.operation": "test_error_operation"},
        scope="oauthclientbridge.db",
    )

    assert data.attributes is not None
    assert data.attributes["db.operation"] == "test_error_operation"
    assert data.attributes["transaction"] is False
    assert data.attributes["db.system"] == "sqlite"
    assert data.attributes["db.name"] == current_settings.database.database
    assert data.attributes["error.type"] == "OperationalError"
    assert data.value == 1


def test_oauth_client_duration_metric_success(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    get: GetClient,
    state: str,
):
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"access_token": "mock_token", "token_type": "Bearer"},
        status_code=200,
    )

    get("/callback?code=1234&state=" + state)

    duration_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.duration",
        HistogramDataPoint,
        attributes={"operation": "token"},
        scope="oauthclientbridge.oauth",
    )
    assert duration_data.attributes is not None
    assert duration_data.attributes["final.result"] == UpstreamResult.SUCCESS
    assert "error.type" not in duration_data.attributes
    assert duration_data.count == 1


def test_oauth_client_retries_metric_success(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    get: GetClient,
    state: str,
):
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"access_token": "mock_token", "token_type": "Bearer"},
        status_code=200,
    )

    get("/callback?code=1234&state=" + state)

    retries_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.retries",
        HistogramDataPoint,
        attributes={"operation": "token"},
        scope="oauthclientbridge.oauth",
    )
    assert retries_data.attributes is not None
    assert retries_data.attributes["final.result"] == UpstreamResult.SUCCESS
    assert "error.type" not in retries_data.attributes
    assert retries_data.sum == 0  # No retries on success
    assert retries_data.count == 1


def test_oauth_client_retries_metric_records_completed_retry_count(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    get: GetClient,
    state: str,
):
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 503, "json": {"error": "temporarily_unavailable"}},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    get("/callback?code=1234&state=" + state)

    retries_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.retries",
        HistogramDataPoint,
        attributes={"operation": "token"},
        scope="oauthclientbridge.oauth",
    )
    assert retries_data.attributes is not None
    assert retries_data.attributes["final.result"] == UpstreamResult.SUCCESS
    assert retries_data.sum == 1
    assert retries_data.count == 1


def test_oauth_client_retries_metric_prometheus_uses_final_status(
    requests_mock: Mocker,
    client: FlaskClient,
):
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 503, "json": {"error": "temporarily_unavailable"}},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    with unittest.mock.patch("time.sleep"):
        oauth.fetch(current_settings.oauth.token_uri, "retry-metric-test")

    metrics_resp = client.get("/metrics")
    body = metrics_resp.data.decode()

    assert body.count('oauth_client_retries_count{endpoint="retry-metric-test"') == 1
    assert (
        'oauth_client_retries_count{endpoint="retry-metric-test",status="http_service_unavailable"}'
        not in body
    )


def test_oauth_client_retry_metrics_record_attempts_and_reasons(
    requests_mock: Mocker,
    client: FlaskClient,
    state: str,
):
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 503, "json": {"error": "temporarily_unavailable"}},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    class FakeRetryLimiter:
        def add(self, tokens: float) -> None:
            self.add_calls = getattr(self, "add_calls", []) + [tokens]

        def consume(self, tokens: float = 1) -> bool:
            return True

    with (
        unittest.mock.patch.object(
            oauth_core, "_get_retry_limiter", return_value=FakeRetryLimiter()
        ),
        unittest.mock.patch("random.uniform", return_value=1.0),
        unittest.mock.patch("time.sleep"),
    ):
        client.get("/callback?code=1234&state=" + state)

    metrics_resp = client.get("/metrics")

    assert b"oauth_client_attempts_total" in metrics_resp.data
    assert b'endpoint="token"' in metrics_resp.data
    assert b'kind="initial"' in metrics_resp.data
    assert b'kind="retry"' in metrics_resp.data
    assert b"oauth_client_retry_decisions_total" in metrics_resp.data
    assert b'decision="retry"' in metrics_resp.data
    assert b'reason="unavailable"' in metrics_resp.data


def test_oauth_client_retry_metrics_bucket_429_as_resource_exhausted(
    requests_mock: Mocker,
    client: FlaskClient,
):
    endpoint = "rate-limit-reason-test"
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 429, "json": {"error": "temporarily_unavailable"}},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    class FakeRetryLimiter:
        def add(self, tokens: float) -> None:
            pass

        def consume(self, tokens: float = 1) -> bool:
            return True

    with (
        unittest.mock.patch.object(
            oauth_core, "_get_retry_limiter", return_value=FakeRetryLimiter()
        ),
        unittest.mock.patch("random.uniform", return_value=1.0),
        unittest.mock.patch("time.sleep"),
    ):
        oauth.fetch(current_settings.oauth.token_uri, endpoint)

    metrics_resp = client.get("/metrics")
    assert b"oauth_client_retry_decisions_total" in metrics_resp.data
    assert b'decision="retry"' in metrics_resp.data
    assert b'reason="resource_exhausted"' in metrics_resp.data


def test_oauth_client_retry_metrics_record_budget_skip(
    requests_mock: Mocker,
    client: FlaskClient,
):
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 503, "json": {"error": "temporarily_unavailable"}},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    class FakeRetryLimiter:
        def add(self, tokens: float) -> None:
            pass

        def consume(self, tokens: float = 1) -> bool:
            return False

    with unittest.mock.patch.object(
        oauth_core, "_get_retry_limiter", return_value=FakeRetryLimiter()
    ):
        oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    metrics_resp = client.get("/metrics")

    assert b"oauth_client_retry_decisions_total" in metrics_resp.data
    assert b'decision="skip"' in metrics_resp.data
    assert b'reason="resource_exhausted"' in metrics_resp.data


def test_oauth_client_error_metric_uses_normalized_retryable_invalid_grant(
    requests_mock: Mocker,
    client: FlaskClient,
):
    endpoint = "retryable-invalid-grant-metric-test"
    requests_mock.post(
        current_settings.oauth.token_uri,
        status_code=503,
        json={"error": OAuthError.INVALID_GRANT},
    )

    oauth.fetch(current_settings.oauth.token_uri, endpoint)

    metrics_resp = client.get("/metrics")

    assert (
        f'oauth_client_error_total{{endpoint="{endpoint}",error="temporarily_unavailable",status="http_service_unavailable"}}'.encode()
        in metrics_resp.data
    )
    assert (
        f'oauth_client_error_total{{endpoint="{endpoint}",error="invalid_grant",status="http_service_unavailable"}}'.encode()
        not in metrics_resp.data
    )


def test_oauth_client_retry_metrics_do_not_count_skipped_retry_attempts(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    client: FlaskClient,
):
    endpoint = "budget-skip-retry-count-test"
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 503, "json": {"error": "temporarily_unavailable"}},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    class FakeRetryLimiter:
        def add(self, tokens: float) -> None:
            pass

        def consume(self, tokens: float = 1) -> bool:
            return False

    with unittest.mock.patch.object(
        oauth_core, "_get_retry_limiter", return_value=FakeRetryLimiter()
    ):
        oauth.fetch(current_settings.oauth.token_uri, endpoint)

    retries_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.retries",
        HistogramDataPoint,
        attributes={"operation": endpoint},
        scope="oauthclientbridge.oauth",
    )
    assert retries_data.sum == 0

    metrics_resp = client.get("/metrics")
    assert (
        f'oauth_client_attempts_total{{endpoint="{endpoint}",kind="retry"}}'.encode()
        not in metrics_resp.data
    )


def test_oauth_client_retry_metrics_record_deadline_skip(
    client: FlaskClient,
):
    current_settings.fetch.total_timeout = 1.0
    current_settings.fetch.total_retries = 2
    current_settings.fetch.backoff_factor = 0.8

    class FakeRetryLimiter:
        def add(self, tokens: float) -> None:
            pass

        def consume(self, tokens: float = 1) -> bool:
            return True

    fake_time = [0.0]

    def now() -> float:
        return fake_time[0]

    def sleep(duration: float) -> None:
        fake_time[0] += duration

    fetch_calls = 0

    def fetch_side_effect(
        span: trace.Span,
        prepared: requests.PreparedRequest,
        timeout: float,
        endpoint: str,
    ) -> tuple[OAuthResponse, HTTPStatus | None, int]:
        _ = span, prepared, timeout, endpoint
        nonlocal fetch_calls
        if fetch_calls == 0:
            fetch_calls += 1
            fake_time[0] += 0.2
            return (
                {"error": "temporarily_unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                0,
            )

        raise AssertionError("unexpected retry attempt")

    with (
        unittest.mock.patch.object(
            oauth_core, "_get_retry_limiter", return_value=FakeRetryLimiter()
        ),
        unittest.mock.patch.object(oauth_core.time, "time", side_effect=now),
        unittest.mock.patch.object(oauth_core.time, "monotonic", side_effect=now),
        unittest.mock.patch.object(oauth_core.time, "sleep", side_effect=sleep),
        unittest.mock.patch.object(oauth_core.random, "uniform", return_value=1.25),
        unittest.mock.patch.object(oauth_core, "_fetch", side_effect=fetch_side_effect),
    ):
        oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    metrics_resp = client.get("/metrics")

    assert b"oauth_client_retry_decisions_total" in metrics_resp.data
    assert b'decision="skip"' in metrics_resp.data
    assert b'reason="deadline_exceeded"' in metrics_resp.data


def test_oauth_client_retry_metrics_record_attempt_limit_skip(
    requests_mock: Mocker,
    client: FlaskClient,
):
    current_settings.fetch.total_retries = 1
    endpoint = "attempt-limit-reason-test"
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"error": "temporarily_unavailable"},
        status_code=503,
    )

    with unittest.mock.patch("time.sleep"):
        oauth.fetch(current_settings.oauth.token_uri, endpoint)

    metrics_resp = client.get("/metrics")

    assert b"oauth_client_retry_decisions_total" in metrics_resp.data
    assert b'decision="skip"' in metrics_resp.data
    assert b'reason="unavailable"' in metrics_resp.data


def test_oauth_client_metrics_failure(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    get: GetClient,
    state: str,
):
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"error": "invalid_grant"},
        status_code=400,
    )

    get("/callback?code=1234&state=" + state)

    duration_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.duration",
        HistogramDataPoint,
        attributes={"operation": "token"},
        scope="oauthclientbridge.oauth",
    )
    assert duration_data.attributes is not None
    assert duration_data.attributes["final.result"] == UpstreamResult.CLIENT_ERROR
    assert duration_data.attributes["error.type"] == "invalid_grant"
    assert duration_data.count == 1


def test_oauth_client_total_metric_success(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    get: GetClient,
    state: str,
):
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"access_token": "mock_token", "token_type": "Bearer"},
        status_code=200,
    )

    get("/callback?code=1234&state=" + state)

    total_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.total",
        NumberDataPoint,
        attributes={"operation": "token"},
        scope="oauthclientbridge.oauth",
    )
    assert total_data.attributes is not None
    assert total_data.attributes["final.result"] == UpstreamResult.SUCCESS
    assert "error.type" not in total_data.attributes
    assert total_data.value == 1


def test_oauth_client_total_metric_failure(
    requests_mock: Mocker,
    otel_mock: otel.OTelMocker,
    get: GetClient,
    state: str,
):
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"error": "invalid_grant"},
        status_code=400,
    )

    get("/callback?code=1234&state=" + state)

    total_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.total",
        NumberDataPoint,
        attributes={"operation": "token"},
        scope="oauthclientbridge.oauth",
    )
    assert total_data.attributes is not None
    assert total_data.attributes["final.result"] == UpstreamResult.CLIENT_ERROR
    assert total_data.attributes["error.type"] == "invalid_grant"
    assert total_data.value == 1
