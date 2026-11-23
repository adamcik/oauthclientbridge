import asyncio
import contextlib
import os
import sqlite3
import unittest.mock
from http import HTTPStatus

import httpx
import pytest
import pytest_sentry_capture
import sentry_sdk
import structlog
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics.export import HistogramDataPoint, NumberDataPoint
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from oauthclientbridge import db, oauth, telemetry, types
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.oauth import (
    _httpx as oauth_httpx,  # pyright: ignore[reportPrivateUsage] # Deterministic retry delays.
)
from oauthclientbridge.oauth._outcome import (  # pyright: ignore[reportPrivateUsage] # Direct implementation test.
    OAuthResponse,
    UpstreamResult,
)
from oauthclientbridge.settings import (
    DatabaseSettings,
    FetchSettings,
    SentrySettings,
    Settings,
    TelemetryComponent,
    TelemetrySettings,
)
from oauthclientbridge.telemetry import _resources as resource_labels
from tests.oauth_server import OAuthServer

from .conftest import GetClient, PostClient, TokenTuple
from .plugins import otel
from .prometheus import sample_value


@pytest.fixture
def database() -> DatabaseSettings:
    return DatabaseSettings(database=":memory:")


@pytest.fixture
def initialize_sentry(
    sentry_transport: pytest_sentry_capture.FakeTransport,
    sentry_isolation_scope: None,
) -> None:
    # The capture plugin owns cleanup after successful initialization.
    try:
        telemetry.init_sentry(
            SentrySettings(
                enabled=True,
                dsn=SecretStr("https://key@example.invalid/1"),
                sample_rate=1.0,
                traces_sample_rate=1.0,
            ),
            sentry_transport,
        )
    except BaseException:
        sentry_sdk.get_client().close()
        raise


def run_fetch(settings: FetchSettings, *args: str, **data: str | None) -> OAuthResponse:
    async def fetch() -> OAuthResponse:
        client = oauth.create_httpx_upstream_client(settings)
        try:
            return await client.fetch(*args, **data)
        finally:
            await client.aclose()

    return asyncio.run(fetch())


logger: structlog.BoundLogger = structlog.get_logger()

TRACE_ID = 0x1234567890ABCDEF
SPAN_ID = 0x123456
HEADER = f"00-{TRACE_ID:032x}-{SPAN_ID:016x}-01"


def test_telemetry_tracer_otel_enabled(
    otel_mock: otel.OTelMocker,
    tracer: trace.Tracer,
) -> None:
    """Verify that our captraces fixture works.

    I.e. we have a global trace provider wired up to this fixture.
    """

    with (
        tracer.start_as_current_span("test-span-1"),
        tracer.start_as_current_span("test-span-2"),
    ):
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


@pytest.mark.parametrize("name", ["http.server.duration", "http.server.size"])
def test_histogram_records_overflow_bucket(
    otel_mock: otel.OTelMocker, meter: metrics.Meter, name: str
) -> None:
    histogram = meter.create_histogram(name)
    histogram.record(1_000_000)

    metric = otel.get_metric(otel_mock.get_metrics_data(), name)
    assert metric is not None
    point = metric.metric.data.data_points[0]
    assert isinstance(point, HistogramDataPoint)
    assert point.explicit_bounds
    assert all(bound < float("inf") for bound in point.explicit_bounds)
    assert point.bucket_counts[-1] == 1
    assert point.count == 1


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


def test_init_metrics_derives_default_instance_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_labels.socket, "gethostname", lambda: "delta")
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


def test_init_metrics_derives_instance_id_without_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_labels.socket, "gethostname", lambda: "delta")
    settings = TelemetrySettings(
        components={TelemetryComponent.METRICS},
        deployment_environment="testing",
        oauth_provider=None,
    )

    with unittest.mock.patch(
        "oauthclientbridge.telemetry._otel.set_meter_provider"
    ) as mock_set_meter_provider:
        telemetry.init_metrics(settings)

    provider = mock_set_meter_provider.call_args.args[0]
    attrs = provider._sdk_config.resource.attributes
    assert attrs["service.instance.id"] == "delta-testing"


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


@pytest.mark.anyio
async def test_httpx_creates_spans(
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    instrumented: None,
    tracer: trace.Tracer,
) -> None:
    upstream_server.expect("/token").respond()

    with tracer.start_as_current_span("test") as parent_span:
        async with httpx.AsyncClient() as client:
            await client.post(upstream_server.url_for("/token"))

    spans = otel_mock.get_finished_spans()
    httpx_span = otel.get_span(spans, "POST")
    assert httpx_span is not None
    otel.assert_trace_id(httpx_span, parent_span)


@pytest.mark.anyio
async def test_httpx_propagates_header(
    upstream_server: OAuthServer,
    instrumented: None,
    tracer: trace.Tracer,
) -> None:
    upstream_server.expect("/token").respond({})

    with tracer.start_as_current_span("parent-span") as parent_span:
        async with httpx.AsyncClient() as client:
            await client.post(upstream_server.url_for("/token"))

    assert len(upstream_server.requests) == 1
    assert "traceparent" in upstream_server.requests[0].headers
    otel.assert_trace_header(
        upstream_server.requests[0].headers["traceparent"],
        parent_span,
    )


def test_sqlite3_creates_spans(
    otel_mock: otel.OTelMocker,
    instrumented: None,
    tracer: trace.Tracer,
) -> None:
    with (
        tracer.start_as_current_span("parent-span") as parent_span,
        sqlite3.connect(":memory:") as conn,
    ):
        # WARNING: `conn.execute()` is not instrumented!
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
    asgi_client: httpx.Client,
) -> None:
    asgi_client.get("/", headers={"traceparent": HEADER})

    spans = otel_mock.get_finished_spans()
    request_span = otel.get_span(spans, "GET /")
    assert request_span is not None
    otel.assert_trace_id(request_span, TRACE_ID)


def test_local_invalid_grant_records_handled_trace_error(
    otel_mock: otel.OTelMocker,
    asgi_post: PostClient,
    access_token: TokenTuple,
    settings: Settings,
) -> None:
    _ = db.update(access_token.client_id, None, settings.database)

    response = asgi_post(
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
    assert request_span.attributes["oauth.error"] == "invalid_grant"
    assert request_span.status.status_code == trace.StatusCode.UNSET
    assert any(event.name == "exception" for event in request_span.events)


def test_oauth_server_error_marks_span_error(
    otel_mock: otel.OTelMocker, tracer: trace.Tracer
) -> None:
    with tracer.start_as_current_span("test"):
        telemetry.record_oauth_error_trace(
            OAuthError.TEMPORARILY_UNAVAILABLE.value,
            "Provider unavailable.",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        )

    span = otel.get_span(otel_mock.get_finished_spans(), "test")
    assert span is not None
    assert span.status.status_code == trace.StatusCode.ERROR


def test_malformed_client_id_records_rejected_value(
    otel_mock: otel.OTelMocker,
    asgi_post: PostClient,
) -> None:
    malformed_client_id = "# SPOTIFY_CLIENT_ID"

    response = asgi_post(
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
    asgi_get: GetClient,
) -> None:
    response = asgi_get("/callback?code=1234")

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
    sentry_capture: pytest_sentry_capture.SentryCapture,
    asgi_get: GetClient,
) -> None:
    asgi_get("/callback?code=secret-code&state=secret-state")

    spans = otel_mock.get_finished_spans()
    request_span = otel.get_span(spans, "GET /callback")
    assert request_span is not None
    assert request_span.attributes is not None
    assert request_span.attributes["http.url"] == (
        "https://bridge.example.com/callback?code=<REDACTED>&state=<REDACTED>"
    )
    assert request_span.attributes["url.full"] == (
        "https://bridge.example.com/callback?code=<REDACTED>&state=<REDACTED>"
    )
    assert request_span.attributes["url.query"] == "code=<REDACTED>&state=<REDACTED>"


def test_authorize_trace_redacts_redirect_location(
    otel_mock: otel.OTelMocker,
    sentry_capture: pytest_sentry_capture.SentryCapture,
    asgi_client: httpx.Client,
) -> None:
    response = asgi_client.get("/")

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
    asgi_client: httpx.Client,
) -> None:
    assert isinstance(asgi_client, TestClient)
    app = asgi_client.app
    assert isinstance(app, Starlette)

    async def boom(_: Request) -> str:
        raise RuntimeError("boom")

    app.add_route("/boom", boom)

    response = asgi_client.get("/boom")

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
    asgi_client: httpx.Client,
    instrumented: None,
) -> None:
    # TODO: test without parent set?
    response = asgi_client.get("/", headers={"traceparent": HEADER})

    assert "traceresponse" in response.headers
    otel.assert_trace_header(response.headers["traceresponse"], TRACE_ID)


def test_endpoint_creates_httpx_spans(
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    instrumented: None,
    asgi_post: PostClient,
    refresh_token: TokenTuple,
    settings: Settings,
) -> None:
    upstream_server.expect("/token").respond(
        {"access_token": "mock_token", "token_type": "Bearer"}
    )

    asgi_post(
        "/token",
        {
            "client_id": refresh_token.client_id,
            "client_secret": refresh_token.client_secret,
            "grant_type": "client_credentials",
        },
        headers={"traceparent": HEADER},
    )

    spans = otel_mock.get_finished_spans()
    outgoing_request_spans = [
        s
        for s in spans
        if s.name == "POST" and s.readable_span.kind == trace.SpanKind.CLIENT
    ]
    assert len(outgoing_request_spans) == 1
    outgoing_request_span = outgoing_request_spans[0]
    otel.assert_trace_id(outgoing_request_span, TRACE_ID)


def test_outgoing_request_span_records_retry_after_header(
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    settings: Settings,
    instrumented: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.fetch.total_attempts = 2
    monkeypatch.setattr(oauth_httpx, "_sleep", unittest.mock.AsyncMock())
    for _ in range(settings.fetch.total_attempts):
        upstream_server.expect("/token").respond(
            {"error": "temporarily_unavailable"},
            status=429,
            headers={"Content-Type": "application/json", "Retry-After": "10"},
        )

    run_fetch(settings.fetch, settings.oauth.token_uri, "test_endpoint")

    spans = otel_mock.get_finished_spans()
    outgoing_request_spans = otel.find_spans(spans, "POST")
    assert len(outgoing_request_spans) == 2
    for outgoing_request_span in outgoing_request_spans:
        assert outgoing_request_span.attributes is not None
        assert outgoing_request_span.attributes[
            "http.response.header.content_length"
        ] == str(len(b'{"error": "temporarily_unavailable"}'))
        assert outgoing_request_span.attributes[
            "http.response.header.content_type"
        ] == ("application/json")
        assert (
            outgoing_request_span.attributes["http.response.header.retry_after"] == "10"
        )


def test_endpoint_creates_sqlite3_spans(
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    instrumented: None,
    asgi_post: PostClient,
    refresh_token: TokenTuple,
    settings: Settings,
) -> None:
    upstream_server.expect("/token").respond(
        {"access_token": "mock_token", "token_type": "Bearer"}
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    asgi_post("/token", data)

    spans = otel_mock.get_finished_spans()
    assert any(
        s.name.startswith("SELECT") or s.name.startswith("UPDATE") for s in spans
    )


def test_token_update_records_changed_fields(
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    instrumented: None,
    asgi_post: PostClient,
    refresh_token: TokenTuple,
    settings: Settings,
) -> None:
    upstream_server.expect("/token").respond(
        {
            "access_token": "mock_token",
            "token_type": "Bearer",
            "refresh_token": "rotated_refresh_token",
        },
    )

    asgi_post(
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
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    instrumented: None,
    asgi_get: GetClient,
    asgi_state: str,
    settings: Settings,
) -> None:
    upstream_server.expect("/token").respond(
        {
            "access_token": "mock_token",
            "refresh_token": "refresh_token",
            "token_type": "Bearer",
        },
    )

    asgi_get("/callback?code=1234&state=" + asgi_state)

    spans = otel_mock.get_finished_spans()
    request_span = otel.get_span(spans, "GET /callback")
    assert request_span is not None
    events = [event for event in request_span.events if event.name == "Inserting token"]

    assert len(events) == 1
    assert events[0].attributes == {"inserted_fields": ("refresh_token",)}


def test_revoked_grant_workaround_adds_span_event(
    otel_mock: otel.OTelMocker,
    asgi_post: PostClient,
    access_token: TokenTuple,
    settings: Settings,
) -> None:
    settings.revoked_grant_workaround_user_agents = r"^Mopidy-Spotify/4\.1\.1\b"

    _ = db.update(access_token.client_id, None, settings.database)

    asgi_post(
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
    upstream_server: OAuthServer,
    instrumented: None,
    asgi_post: PostClient,
    refresh_token: TokenTuple,
    settings: Settings,
) -> None:
    upstream_server.expect("/token").respond(
        {"access_token": "mock_token", "token_type": "Bearer"}
    )

    asgi_post(
        "/token",
        {
            "client_id": refresh_token.client_id,
            "client_secret": refresh_token.client_secret,
            "grant_type": "client_credentials",
        },
        headers={"traceparent": HEADER},
    )

    history = upstream_server.requests
    assert len(history) == 1
    assert "traceparent" in history[0].headers
    otel.assert_trace_header(history[0].headers["traceparent"], TRACE_ID)


def test_asgi_metrics(
    otel_mock: otel.OTelMocker,
    asgi_client: httpx.Client,
) -> None:
    asgi_client.get("/")

    metrics = otel_mock.get_metrics_data()
    assert otel.get_metric(metrics, "http.server.duration") is not None
    assert otel.get_metric(metrics, "http.server.active_requests") is not None


@pytest.mark.anyio
async def test_httpx_metrics(
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    instrumented: None,
) -> None:
    upstream_server.expect("/token").respond()

    async with httpx.AsyncClient() as client:
        await client.post(upstream_server.url_for("/token"))

    metrics = otel_mock.get_metrics_data()
    assert otel.get_metric(metrics, "http.client.duration") is not None


# NOTE: As of 2025-08-01 the sqlite3 otel auto instrumentation does not
# support metrics. So we've added our own...
def test_db_cursor_duration_metric(
    database: DatabaseSettings,
    otel_mock: otel.OTelMocker,
):
    with (
        contextlib.closing(sqlite3.connect(database.database)) as connection,
        db.cursor(
            types.DatabaseOperation.INSERT_TOKEN,
            transaction=True,
            connection=connection,
            database_name=database.database,
        ) as c,
    ):
        # Perform some dummy DB operations using the provided cursor 'c'
        c.execute("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY)")
        c.execute("INSERT INTO test_table (id) VALUES (1)")

    metrics = otel_mock.get_metrics_data()
    data = otel.latest_metric_data(
        metrics,
        "oauth.db.cursor.duration",
        HistogramDataPoint,
        attributes={"db.operation": "insert_token"},
        scope="oauthclientbridge.db",
    )

    assert data.attributes is not None
    assert data.attributes["db.operation"] == "insert_token"
    assert "error.type" not in data.attributes
    assert data.count == 1


def test_db_cursor_count_metric_success(
    database: DatabaseSettings,
    otel_mock: otel.OTelMocker,
):
    with (
        contextlib.closing(sqlite3.connect(database.database)) as connection,
        db.cursor(
            types.DatabaseOperation.INSERT_TOKEN,
            transaction=True,
            connection=connection,
            database_name=database.database,
        ) as c,
    ):
        c.execute("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY)")
        c.execute("INSERT INTO test_table (id) VALUES (1)")

    metrics = otel_mock.get_metrics_data()
    data = otel.latest_metric_data(
        metrics,
        "oauth.db.cursor.total",
        NumberDataPoint,
        attributes={"db.operation": "insert_token"},
        scope="oauthclientbridge.db",
    )

    assert data.attributes is not None
    assert data.attributes["db.operation"] == "insert_token"
    assert data.attributes["transaction"] is True
    assert data.attributes["db.system"] == "sqlite"
    assert data.attributes["db.name"] == database.database
    assert "error.type" not in data.attributes
    assert data.value == 1


def test_db_cursor_duration_metric_error(
    database: DatabaseSettings,
    otel_mock: otel.OTelMocker,
):
    with (
        contextlib.closing(sqlite3.connect(database.database)) as connection,
        pytest.raises(sqlite3.Error),
        db.cursor(
            types.DatabaseOperation.LOOKUP_TOKEN,
            connection=connection,
            database_name=database.database,
        ) as c,
    ):
        c.execute("INVALID SQL QUERY")

    metrics = otel_mock.get_metrics_data()
    data = otel.latest_metric_data(
        metrics,
        "oauth.db.cursor.duration",
        HistogramDataPoint,
        attributes={"db.operation": "lookup_token"},
        scope="oauthclientbridge.db",
    )

    assert data.attributes is not None
    assert data.attributes["db.operation"] == "lookup_token"
    assert data.attributes["error.type"] == "OperationalError"
    assert data.count == 1


def test_db_cursor_count_metric_error(
    database: DatabaseSettings,
    otel_mock: otel.OTelMocker,
):
    with (
        contextlib.closing(sqlite3.connect(database.database)) as connection,
        pytest.raises(sqlite3.Error),
        db.cursor(
            types.DatabaseOperation.LOOKUP_TOKEN,
            connection=connection,
            database_name=database.database,
        ) as c,
    ):
        c.execute("INVALID SQL QUERY")

    metrics = otel_mock.get_metrics_data()
    data = otel.latest_metric_data(
        metrics,
        "oauth.db.cursor.total",
        NumberDataPoint,
        attributes={"db.operation": "lookup_token"},
        scope="oauthclientbridge.db",
    )

    assert data.attributes is not None
    assert data.attributes["db.operation"] == "lookup_token"
    assert data.attributes["transaction"] is False
    assert data.attributes["db.system"] == "sqlite"
    assert data.attributes["db.name"] == database.database
    assert data.attributes["error.type"] == "OperationalError"
    assert data.value == 1


def test_oauth_client_duration_metric_success(
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    asgi_get: GetClient,
    asgi_state: str,
    settings: Settings,
):
    upstream_server.expect("/token").respond(
        {"access_token": "mock_token", "token_type": "Bearer"}
    )

    asgi_get("/callback?code=1234&state=" + asgi_state)

    duration_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.duration",
        HistogramDataPoint,
        attributes={"operation": "authorization_code"},
        scope="oauthclientbridge.oauth",
    )
    assert duration_data.attributes is not None
    assert duration_data.attributes["final.result"] == UpstreamResult.SUCCESS
    assert "error.type" not in duration_data.attributes
    assert duration_data.count == 1


def test_oauth_client_retries_metric_success(
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    asgi_get: GetClient,
    asgi_state: str,
    settings: Settings,
):
    upstream_server.expect("/token").respond(
        {"access_token": "mock_token", "token_type": "Bearer"}
    )

    asgi_get("/callback?code=1234&state=" + asgi_state)

    retries_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.retries",
        HistogramDataPoint,
        attributes={"operation": "authorization_code"},
        scope="oauthclientbridge.oauth",
    )
    assert retries_data.attributes is not None
    assert retries_data.attributes["final.result"] == UpstreamResult.SUCCESS
    assert "error.type" not in retries_data.attributes
    assert retries_data.sum == 0  # No retries on success
    assert retries_data.count == 1


def test_oauth_client_retries_metric_records_completed_retry_count(
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    asgi_get: GetClient,
    asgi_state: str,
    settings: Settings,
):
    upstream_server.expect("/token").respond(
        {"error": "temporarily_unavailable"}, status=503
    )
    upstream_server.expect("/token").respond(
        {"access_token": "mock_token", "token_type": "Bearer"}
    )

    asgi_get("/callback?code=1234&state=" + asgi_state)

    retries_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.retries",
        HistogramDataPoint,
        attributes={"operation": "authorization_code"},
        scope="oauthclientbridge.oauth",
    )
    assert retries_data.attributes is not None
    assert retries_data.attributes["final.result"] == UpstreamResult.SUCCESS
    assert retries_data.sum == 1
    assert retries_data.count == 1


def test_oauth_client_retries_metric_prometheus_uses_final_status(
    upstream_server: OAuthServer,
    settings: Settings,
):
    upstream_server.expect("/token").respond(
        {"error": "temporarily_unavailable"}, status=503
    )
    upstream_server.expect("/token").respond(
        {"access_token": "mock_token", "token_type": "Bearer"}
    )

    settings.fetch.backoff_factor = 0
    run_fetch(settings.fetch, settings.oauth.token_uri, "retry-metric-test")

    body = telemetry.export_metrics(settings.prometheus).decode()

    assert body.count('oauth_client_retries_count{endpoint="retry-metric-test"') == 1
    assert (
        'oauth_client_retries_count{endpoint="retry-metric-test",status="http_service_unavailable"}'
        not in body
    )


def test_oauth_client_retry_metrics_record_attempts_and_reasons(
    upstream_server: OAuthServer,
    asgi_client: httpx.Client,
    asgi_state: str,
    settings: Settings,
):
    upstream_server.expect("/token").respond(
        {"error": "temporarily_unavailable"}, status=503
    )
    upstream_server.expect("/token").respond(
        {"access_token": "mock_token", "token_type": "Bearer"}
    )

    settings.fetch.backoff_factor = 0
    asgi_client.get("/callback?code=1234&state=" + asgi_state)

    metrics_resp = asgi_client.get("/metrics")

    assert b"oauth_client_attempts_total" in metrics_resp.content
    assert b'endpoint="authorization_code"' in metrics_resp.content
    assert b'kind="initial"' in metrics_resp.content
    assert b'kind="retry"' in metrics_resp.content
    assert b"oauth_client_retry_decisions_total" in metrics_resp.content
    assert b'decision="retry"' in metrics_resp.content
    assert b'reason="unavailable"' in metrics_resp.content


def test_oauth_client_retry_metrics_bucket_429_as_resource_exhausted(
    upstream_server: OAuthServer,
    settings: Settings,
):
    endpoint = "rate-limit-reason-test"
    upstream_server.expect("/token").respond(
        {"error": "temporarily_unavailable"}, status=429
    )
    upstream_server.expect("/token").respond(
        {"access_token": "mock_token", "token_type": "Bearer"}
    )

    settings.fetch.backoff_factor = 0
    run_fetch(settings.fetch, settings.oauth.token_uri, endpoint)

    body = telemetry.export_metrics(settings.prometheus)
    assert b"oauth_client_retry_decisions_total" in body
    assert b'decision="retry"' in body
    assert b'reason="resource_exhausted"' in body


def test_oauth_client_error_metric_uses_normalized_retryable_invalid_grant(
    upstream_server: OAuthServer,
    settings: Settings,
):
    endpoint = "retryable-invalid-grant-metric-test"
    labels = {
        "endpoint": endpoint,
        "error": "temporarily_unavailable",
        "status": "http_service_unavailable",
    }
    before = sample_value(
        telemetry.export_metrics(settings.prometheus),
        "oauth_client_error_total",
        labels,
    )
    for _ in range(settings.fetch.total_attempts):
        upstream_server.expect("/token").respond(
            {"error": OAuthError.INVALID_GRANT}, status=503
        )

    run_fetch(settings.fetch, settings.oauth.token_uri, endpoint)

    body = telemetry.export_metrics(settings.prometheus)

    assert sample_value(body, "oauth_client_error_total", labels) == (
        before + settings.fetch.total_attempts
    )
    assert (
        f'oauth_client_error_total{{endpoint="{endpoint}",error="invalid_grant",status="http_service_unavailable"}}'.encode()
        not in body
    )


def test_oauth_client_retry_metrics_record_deadline_skip(
    upstream_server: OAuthServer,
    settings: Settings,
):
    settings.fetch.total_timeout = 1.0
    settings.fetch.total_attempts = 3
    upstream_server.expect("/token").respond(
        {"error": "temporarily_unavailable"}, status=503, headers={"Retry-After": "10"}
    )

    run_fetch(settings.fetch, settings.oauth.token_uri, "test_endpoint")

    assert len(upstream_server.requests) == 1
    body = telemetry.export_metrics(settings.prometheus)

    assert b"oauth_client_retry_decisions_total" in body
    assert b'decision="skip"' in body
    assert b'reason="deadline_exceeded"' in body


def test_oauth_client_retry_metrics_record_attempt_limit_skip(
    upstream_server: OAuthServer,
    settings: Settings,
):
    settings.fetch.total_attempts = 2
    endpoint = "attempt-limit-reason-test"
    labels = {"endpoint": endpoint, "decision": "skip", "reason": "unavailable"}
    before = sample_value(
        telemetry.export_metrics(settings.prometheus),
        "oauth_client_retry_decisions_total",
        labels,
    )
    for _ in range(settings.fetch.total_attempts):
        upstream_server.expect("/token").respond(
            {"error": "temporarily_unavailable"}, status=503
        )

    settings.fetch.backoff_factor = 0
    run_fetch(settings.fetch, settings.oauth.token_uri, endpoint)

    body = telemetry.export_metrics(settings.prometheus)

    assert (
        sample_value(body, "oauth_client_retry_decisions_total", labels) == before + 1
    )


def test_oauth_client_metrics_failure(
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    asgi_get: GetClient,
    asgi_state: str,
    settings: Settings,
):
    upstream_server.expect("/token").respond({"error": "invalid_grant"}, status=400)

    asgi_get("/callback?code=1234&state=" + asgi_state)

    duration_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.duration",
        HistogramDataPoint,
        attributes={"operation": "authorization_code"},
        scope="oauthclientbridge.oauth",
    )
    assert duration_data.attributes is not None
    assert duration_data.attributes["final.result"] == UpstreamResult.CLIENT_ERROR
    assert duration_data.attributes["error.type"] == "invalid_grant"
    assert duration_data.count == 1


def test_oauth_client_total_metric_success(
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    asgi_get: GetClient,
    asgi_state: str,
    settings: Settings,
):
    upstream_server.expect("/token").respond(
        {"access_token": "mock_token", "token_type": "Bearer"}
    )

    asgi_get("/callback?code=1234&state=" + asgi_state)

    total_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.total",
        NumberDataPoint,
        attributes={"operation": "authorization_code"},
        scope="oauthclientbridge.oauth",
    )
    assert total_data.attributes is not None
    assert total_data.attributes["final.result"] == UpstreamResult.SUCCESS
    assert "error.type" not in total_data.attributes
    assert total_data.value == 1


def test_oauth_client_total_metric_failure(
    upstream_server: OAuthServer,
    otel_mock: otel.OTelMocker,
    asgi_get: GetClient,
    asgi_state: str,
    settings: Settings,
):
    upstream_server.expect("/token").respond({"error": "invalid_grant"}, status=400)

    asgi_get("/callback?code=1234&state=" + asgi_state)

    total_data = otel.latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.total",
        NumberDataPoint,
        attributes={"operation": "authorization_code"},
        scope="oauthclientbridge.oauth",
    )
    assert total_data.attributes is not None
    assert total_data.attributes["final.result"] == UpstreamResult.CLIENT_ERROR
    assert total_data.attributes["error.type"] == "invalid_grant"
    assert total_data.value == 1
