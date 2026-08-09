from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import anyio
import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import HistogramDataPoint, NumberDataPoint

from oauthclientbridge import oauth, telemetry, types
from oauthclientbridge.oauth import (
    _httpx as httpx_implementation,  # pyright: ignore[reportPrivateUsage] # Deterministic retry-delay cancellation.
)
from oauthclientbridge.settings import (
    ClientResetError,
    FetchSettings,
    PrometheusSettings,
)
from oauthclientbridge.telemetry import (
    _prometheus as stats,  # pyright: ignore[reportPrivateUsage] # Metric emission contract.
)
from pytest_otel_capture import (
    OTelMocker,
    assert_trace_header,
    assert_trace_id,
    latest_metric_data,
)
from tests.oauth_server import OAuthServer


@pytest.mark.anyio
async def test_httpx_fetcher_preserves_upstream_telemetry(
    oauth_server: OAuthServer,
    otel_mock: OTelMocker,
    instrumented: None,
) -> None:
    oauth_server.expect("/token").respond(
        {"access_token": "provider-token", "token_type": "Bearer"}
    )
    client = oauth.create_httpx_upstream_client(FetchSettings())

    try:
        with trace.get_tracer("tests").start_as_current_span("fetch upstream token"):
            result = await client.fetch(
                oauth_server.url_for("/token"),
                types.UpstreamGrantType.AUTHORIZATION_CODE,
            )
    finally:
        await client.aclose()

    assert result == {"access_token": "provider-token", "token_type": "Bearer"}
    spans = otel_mock.get_finished_spans()
    parent_span = next(span for span in spans if span.name == "fetch upstream token")
    assert_trace_header(
        oauth_server.requests[0].headers["traceparent"], parent_span.trace_id
    )
    assert_trace_id(
        next(span for span in spans if span.name == "POST"), parent_span.trace_id
    )
    attributes = {
        "operation": "authorization_code",
        "final.result": "success",
    }
    total = latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.total",
        NumberDataPoint,
        attributes=attributes,
        scope="oauthclientbridge.oauth",
    )
    duration = latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.duration",
        HistogramDataPoint,
        attributes=attributes,
        scope="oauthclientbridge.oauth",
    )
    retries = latest_metric_data(
        otel_mock.get_metrics_data(),
        "oauth.client.retries",
        HistogramDataPoint,
        attributes=attributes,
        scope="oauthclientbridge.oauth",
    )
    assert total.value == 1
    assert duration.count == 1
    assert retries.sum == 0

    metrics = telemetry.export_metrics(PrometheusSettings())
    assert (
        b'oauth_client_attempts_total{endpoint="authorization_code",kind="initial"}'
        in metrics
    )
    assert (
        b'oauth_client_latency_seconds_count{endpoint="authorization_code",status="http_ok"}'
        in metrics
    )
    assert (
        b'oauth_client_response_bytes_count{endpoint="authorization_code",status="http_ok"}'
        in metrics
    )
    assert (
        b'oauth_client_retries_count{endpoint="authorization_code",status="http_ok"}'
        in metrics
    )


@pytest.mark.anyio
async def test_fetch_tracker_cancels_remaining_work_after_deadline() -> None:
    fetches = httpx_implementation._FetchTracker()  # pyright: ignore[reportPrivateUsage] # Focused lifecycle contract.
    started = anyio.Event()
    cancelled = anyio.Event()

    async def fetch() -> None:
        try:
            async with fetches.track():
                started.set()
                await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            cancelled.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(fetch)
        await started.wait()

        await fetches.aclose(0)
        await cancelled.wait()

        with pytest.raises(RuntimeError, match="shutting down"):
            async with fetches.track():
                pass


@pytest.mark.anyio
async def test_fetch_tracker_drains_work_completed_before_deadline() -> None:
    fetches = httpx_implementation._FetchTracker()  # pyright: ignore[reportPrivateUsage] # Focused lifecycle contract.
    started = anyio.Event()
    release = anyio.Event()
    completed = anyio.Event()
    closed = anyio.Event()

    async def fetch() -> None:
        async with fetches.track():
            started.set()
            await release.wait()
        completed.set()

    async def close() -> None:
        await fetches.aclose(1)
        closed.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(fetch)
        await started.wait()
        task_group.start_soon(close)
        await fetches._closing_started.wait()  # pyright: ignore[reportPrivateUsage] # Lifecycle synchronization.

        with pytest.raises(RuntimeError, match="shutting down"):
            async with fetches.track():
                pass
        assert not closed.is_set()

        release.set()
        await completed.wait()
        await closed.wait()


@pytest.mark.anyio
async def test_httpx_fetcher_retries_retryable_upstream_failure(
    oauth_server: OAuthServer,
    otel_mock: OTelMocker,
) -> None:
    oauth_server.expect("/token").respond(status=HTTPStatus.SERVICE_UNAVAILABLE)
    oauth_server.expect("/token").respond(
        {"access_token": "provider-token", "token_type": "Bearer"}
    )
    client = oauth.create_httpx_upstream_client(
        FetchSettings(total_attempts=2, backoff_factor=0)
    )

    try:
        with trace.get_tracer("tests").start_as_current_span("fetch upstream token"):
            result = await client.fetch(
                oauth_server.url_for("/token"),
                types.UpstreamGrantType.AUTHORIZATION_CODE,
            )
    finally:
        await client.aclose()

    assert result == {"access_token": "provider-token", "token_type": "Bearer"}
    assert len(oauth_server.requests) == 2
    metrics = telemetry.export_metrics(PrometheusSettings())
    assert (
        b'oauth_client_generation_resets_total{endpoint="authorization_code",error="http_503"} 1.0'
        in metrics
    )
    assert b"oauth_client_generation_drain_seconds_count 1.0" in metrics
    span = next(
        span
        for span in otel_mock.get_finished_spans()
        if span.name == "fetch upstream token"
    )
    events = [event for event in span.events if event.name == "Client generation reset"]
    assert len(events) == 1
    assert events[0].attributes == {
        "oauth.upstream_grant_type": "authorization_code",
        "client.reset_error": "http_503",
    }


@pytest.mark.anyio
async def test_httpx_fetcher_skips_retry_when_budget_is_exhausted(
    oauth_server: OAuthServer,
    otel_mock: OTelMocker,
) -> None:
    oauth_server.expect("/token").respond(status=HTTPStatus.SERVICE_UNAVAILABLE)
    client = oauth.create_httpx_upstream_client(
        FetchSettings(total_attempts=2, backoff_factor=0, retry_budget_capacity=0)
    )
    reset_counter = stats.ClientGenerationResetCounter.labels(
        endpoint=types.UpstreamGrantType.AUTHORIZATION_CODE,
        error=ClientResetError.HTTP_503,
    )
    reset_count = reset_counter._value.get()  # pyright: ignore[reportPrivateUsage] # Metric value assertion.

    try:
        with trace.get_tracer("tests").start_as_current_span("fetch upstream token"):
            result = await client.fetch(
                oauth_server.url_for("/token"),
                types.UpstreamGrantType.AUTHORIZATION_CODE,
            )
    finally:
        await client.aclose()

    assert result == {
        "error": "temporarily_unavailable",
        "error_description": "Provider is unavailable.",
    }
    assert len(oauth_server.requests) == 1
    assert reset_counter._value.get() == reset_count  # pyright: ignore[reportPrivateUsage] # Metric value assertion.
    span = next(
        span
        for span in otel_mock.get_finished_spans()
        if span.name == "fetch upstream token"
    )
    assert all(event.name != "Client generation reset" for event in span.events)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        pytest.param(
            FetchSettings(
                total_attempts=2,
                backoff_factor=0,
                retry_budget_capacity=0,
            ),
            "budget_exhausted",
            id="budget exhausted",
        ),
        pytest.param(
            FetchSettings(
                total_attempts=2,
                total_timeout=0.1,
                backoff_factor=0,
            ),
            "deadline_exceeded",
            id="deadline exhausted",
        ),
    ],
)
async def test_httpx_fetcher_records_suppressed_retry_decision(
    settings: FetchSettings,
    reason: str,
    oauth_server: OAuthServer,
    otel_mock: OTelMocker,
) -> None:
    oauth_server.expect("/token").respond(
        {}, status=HTTPStatus.SERVICE_UNAVAILABLE, headers={"Retry-After": "1"}
    )
    client = oauth.create_httpx_upstream_client(settings)
    tracer = trace.get_tracer("tests")

    try:
        with tracer.start_as_current_span("fetch upstream token"):
            await client.fetch(
                oauth_server.url_for("/token"), types.UpstreamGrantType.REFRESH_TOKEN
            )
    finally:
        await client.aclose()

    metrics = telemetry.export_metrics(PrometheusSettings())
    assert (
        b'oauth_client_retry_decisions_total{decision="skip",endpoint="refresh_token",reason="'
        + reason.encode()
        + b'"} 1.0'
    ) in metrics
    span = next(
        span
        for span in otel_mock.get_finished_spans()
        if span.name == "fetch upstream token"
    )
    events = [event for event in span.events if event.name == "Retry suppressed"]
    assert len(events) == 1
    assert events[0].attributes == {
        "oauth.upstream_grant_type": "refresh_token",
        "retry.reason": reason,
    }
    assert oauth_server.url_for("/token") not in str(events[0].attributes)


@pytest.mark.anyio
async def test_httpx_fetcher_records_deadline_suppression_without_delay(
    monkeypatch: pytest.MonkeyPatch,
    oauth_server: OAuthServer,
    otel_mock: OTelMocker,
) -> None:
    current_times = iter((0.0, 1.0))
    monkeypatch.setattr(
        httpx_implementation.anyio, "current_time", lambda: next(current_times)
    )
    oauth_server.expect("/token").respond({}, status=HTTPStatus.SERVICE_UNAVAILABLE)
    client = oauth.create_httpx_upstream_client(
        FetchSettings(total_attempts=2, total_timeout=0.5, backoff_factor=0)
    )
    tracer = trace.get_tracer("tests")

    try:
        with tracer.start_as_current_span("fetch upstream token"):
            await client.fetch(
                oauth_server.url_for("/token"), types.UpstreamGrantType.REFRESH_TOKEN
            )
    finally:
        await client.aclose()

    span = next(
        span
        for span in otel_mock.get_finished_spans()
        if span.name == "fetch upstream token"
    )
    assert [event.name for event in span.events] == ["Retry suppressed"]
    assert len(oauth_server.requests) == 1


@pytest.mark.anyio
async def test_httpx_fetcher_limits_requests_to_total_attempts(
    oauth_server: OAuthServer,
) -> None:
    for _ in range(3):
        oauth_server.expect("/token").respond({}, status=HTTPStatus.SERVICE_UNAVAILABLE)
    client = oauth.create_httpx_upstream_client(
        FetchSettings(total_attempts=3, backoff_factor=0)
    )

    try:
        await client.fetch(
            oauth_server.url_for("/token"),
            types.UpstreamGrantType.AUTHORIZATION_CODE,
        )
    finally:
        await client.aclose()

    assert len(oauth_server.requests) == 3


@pytest.mark.anyio
async def test_httpx_fetcher_retries_read_timeout_when_grant_type_is_configured(
    oauth_server: OAuthServer,
) -> None:
    request_started = Event()
    release_request = Event()
    oauth_server.expect("/token").respond(
        request_started=request_started,
        hold_until=release_request,
    )
    oauth_server.expect("/token").respond(
        {"access_token": "provider-token", "token_type": "Bearer"}
    )
    client = oauth.create_httpx_upstream_client(
        FetchSettings(
            total_attempts=2,
            timeout=0.1,
            backoff_factor=0,
            read_timeout_retry_grant_types=(
                types.UpstreamGrantType.AUTHORIZATION_CODE,
            ),
        )
    )

    result: dict[str, object] | None = None

    async def fetch() -> None:
        nonlocal result
        result = await client.fetch(
            oauth_server.url_for("/token"),
            types.UpstreamGrantType.AUTHORIZATION_CODE,
        )

    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(fetch)
            assert await anyio.to_thread.run_sync(request_started.wait, 1)
    finally:
        release_request.set()
        await client.aclose()

    assert result is not None
    assert result == {"access_token": "provider-token", "token_type": "Bearer"}
    assert len(oauth_server.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "upstream_grant_type",
    [
        types.UpstreamGrantType.AUTHORIZATION_CODE,
        types.UpstreamGrantType.REFRESH_TOKEN,
    ],
)
async def test_httpx_fetcher_does_not_retry_read_timeout_by_default(
    upstream_grant_type: types.UpstreamGrantType,
    oauth_server: OAuthServer,
) -> None:
    request_started = Event()
    release_request = Event()
    oauth_server.expect("/token").respond(
        request_started=request_started,
        hold_until=release_request,
    )
    client = oauth.create_httpx_upstream_client(
        FetchSettings(total_attempts=2, timeout=0.1, backoff_factor=0)
    )

    result: dict[str, object] | None = None

    async def fetch() -> None:
        nonlocal result
        result = await client.fetch(oauth_server.url_for("/token"), upstream_grant_type)

    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(fetch)
            assert await anyio.to_thread.run_sync(request_started.wait, 1)
    finally:
        release_request.set()
        await client.aclose()

    assert result is not None
    assert result == {
        "error": "temporarily_unavailable",
        "error_description": "Request timed out while reading from provider.",
    }
    assert len(oauth_server.requests) == 1


@pytest.mark.anyio
async def test_httpx_fetcher_counts_pool_saturation_without_retrying(
    oauth_server: OAuthServer,
) -> None:
    request_started = Event()
    release_request = Event()
    oauth_server.expect("/token").respond(
        request_started=request_started,
        hold_until=release_request,
    )
    oauth_server.expect("/token").respond(
        {"access_token": "provider-token", "token_type": "Bearer"}
    )
    client = oauth.create_httpx_upstream_client(
        FetchSettings(
            total_attempts=3,
            total_timeout=1,
            pool_max_connections=1,
            pool_max_keepalive_connections=1,
            pool_timeout=0.1,
        )
    )

    async def occupy_connection() -> None:
        await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.AUTHORIZATION_CODE
        )

    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(occupy_connection)
            assert await anyio.to_thread.run_sync(request_started.wait, 1)
            result = await client.fetch(
                oauth_server.url_for("/token"),
                types.UpstreamGrantType.AUTHORIZATION_CODE,
            )
            assert len(oauth_server.requests) == 1
            release_request.set()
        subsequent_result = await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.AUTHORIZATION_CODE
        )
    finally:
        release_request.set()
        await client.aclose()

    assert result == {
        "error": "temporarily_unavailable",
        "error_description": "Provider connection pool is unavailable.",
    }
    assert subsequent_result == {
        "access_token": "provider-token",
        "token_type": "Bearer",
    }
    assert len({request.client_address for request in oauth_server.requests}) == 1
    assert (
        b'oauth_client_error_total{endpoint="authorization_code",error="pool_saturation",status="unknown"}'
        b" 1.0" in telemetry.export_metrics(PrometheusSettings())
    )


@pytest.mark.anyio
async def test_httpx_fetcher_does_not_spend_budget_on_deadline_skipped_retry(
    oauth_server: OAuthServer,
) -> None:
    oauth_server.expect("/token").respond(
        {}, status=HTTPStatus.SERVICE_UNAVAILABLE, headers={"Retry-After": "1"}
    )
    for _ in range(2):
        oauth_server.expect("/token").respond({}, status=HTTPStatus.SERVICE_UNAVAILABLE)
    client = oauth.create_httpx_upstream_client(
        FetchSettings(
            total_attempts=2,
            total_timeout=0.5,
            backoff_factor=0,
            retry_budget_capacity=1,
            retry_budget_refill_per_initial=0,
        )
    )

    try:
        await client.fetch(
            oauth_server.url_for("/token"),
            types.UpstreamGrantType.AUTHORIZATION_CODE,
        )
        await client.fetch(
            oauth_server.url_for("/token"),
            types.UpstreamGrantType.AUTHORIZATION_CODE,
        )
    finally:
        await client.aclose()

    assert len(oauth_server.requests) == 3


@pytest.mark.anyio
@pytest.mark.parametrize(
    "upstream_grant_type",
    [
        types.UpstreamGrantType.AUTHORIZATION_CODE,
        types.UpstreamGrantType.REFRESH_TOKEN,
    ],
)
async def test_httpx_fetcher_rejects_non_success_token_payload(
    upstream_grant_type: types.UpstreamGrantType,
    oauth_server: OAuthServer,
) -> None:
    oauth_server.expect("/token").respond(
        {"access_token": "provider-token", "token_type": "Bearer"},
        status=HTTPStatus.BAD_REQUEST,
    )
    client = oauth.create_httpx_upstream_client(FetchSettings(total_attempts=1))

    try:
        result = await client.fetch(oauth_server.url_for("/token"), upstream_grant_type)
    finally:
        await client.aclose()

    assert result["error"] == "server_error"
    assert "access_token" not in result


@pytest.mark.anyio
@pytest.mark.parametrize(
    "upstream_grant_type",
    [
        types.UpstreamGrantType.AUTHORIZATION_CODE,
        types.UpstreamGrantType.REFRESH_TOKEN,
    ],
)
async def test_httpx_fetcher_rejects_redirect(
    upstream_grant_type: types.UpstreamGrantType,
    oauth_server: OAuthServer,
) -> None:
    oauth_server.expect("/token").respond(
        status=HTTPStatus.FOUND,
        headers={"Location": "https://untrusted.example.com/token"},
    )
    client = oauth.create_httpx_upstream_client(FetchSettings(total_attempts=1))

    try:
        result = await client.fetch(oauth_server.url_for("/token"), upstream_grant_type)
    finally:
        await client.aclose()

    assert result["error"] == "server_error"


@pytest.mark.anyio
async def test_httpx_fetcher_preserves_retryable_response_when_retry_delay_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    oauth_server: OAuthServer,
) -> None:
    async def cancel_retry_delay(_: float) -> None:
        raise TimeoutError

    monkeypatch.setattr(httpx_implementation, "_sleep", cancel_retry_delay)
    oauth_server.expect("/token").respond(
        status=HTTPStatus.SERVICE_UNAVAILABLE, headers={"Retry-After": "1"}
    )
    client = oauth.create_httpx_upstream_client(FetchSettings(total_attempts=2))

    try:
        result = await client.fetch(
            oauth_server.url_for("/token"),
            types.UpstreamGrantType.AUTHORIZATION_CODE,
        )
    finally:
        await client.aclose()

    assert result == {
        "error": "temporarily_unavailable",
        "error_description": "Provider is unavailable.",
        "retry_after": 1,
    }
    assert len(oauth_server.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "upstream_grant_type",
    [
        types.UpstreamGrantType.AUTHORIZATION_CODE,
        types.UpstreamGrantType.REFRESH_TOKEN,
    ],
)
async def test_httpx_fetcher_reuses_http11_connection(
    upstream_grant_type: types.UpstreamGrantType,
    oauth_server: OAuthServer,
) -> None:
    for _ in range(2):
        oauth_server.expect("/token").respond(
            {"access_token": "provider-token", "token_type": "Bearer"}
        )
    client = oauth.create_httpx_upstream_client(FetchSettings(total_attempts=1))

    try:
        for _ in range(2):
            result = await client.fetch(
                oauth_server.url_for("/token"), upstream_grant_type
            )
            assert result == {"access_token": "provider-token", "token_type": "Bearer"}
    finally:
        await client.aclose()

    assert len({request.client_address for request in oauth_server.requests}) == 1


@pytest.mark.anyio
async def test_httpx_fetcher_rotates_connection_for_admitted_gateway_retry(
    oauth_server: OAuthServer,
) -> None:
    oauth_server.expect("/token").respond(status=HTTPStatus.SERVICE_UNAVAILABLE)
    oauth_server.expect("/token").respond(
        {"access_token": "provider-token", "token_type": "Bearer"}
    )
    client = oauth.create_httpx_upstream_client(
        FetchSettings(total_attempts=2, backoff_factor=0)
    )

    try:
        result = await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.AUTHORIZATION_CODE
        )
    finally:
        await client.aclose()

    assert result == {"access_token": "provider-token", "token_type": "Bearer"}
    assert len({request.client_address for request in oauth_server.requests}) == 2


@pytest.mark.anyio
async def test_httpx_fetcher_retains_connection_for_too_many_requests_retry(
    oauth_server: OAuthServer,
) -> None:
    oauth_server.expect("/token").respond(status=HTTPStatus.TOO_MANY_REQUESTS)
    oauth_server.expect("/token").respond(
        {"access_token": "provider-token", "token_type": "Bearer"}
    )
    client = oauth.create_httpx_upstream_client(
        FetchSettings(total_attempts=2, backoff_factor=0)
    )

    try:
        result = await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.AUTHORIZATION_CODE
        )
    finally:
        await client.aclose()

    assert result == {"access_token": "provider-token", "token_type": "Bearer"}
    assert len({request.client_address for request in oauth_server.requests}) == 1


@pytest.mark.anyio
async def test_httpx_fetcher_does_not_rotate_connection_when_budget_is_exhausted(
    oauth_server: OAuthServer,
) -> None:
    oauth_server.expect("/token").respond(status=HTTPStatus.SERVICE_UNAVAILABLE)
    oauth_server.expect("/token").respond(
        {"access_token": "provider-token", "token_type": "Bearer"}
    )
    client = oauth.create_httpx_upstream_client(
        FetchSettings(
            total_attempts=2,
            backoff_factor=0,
            retry_budget_capacity=0,
        )
    )

    try:
        await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.AUTHORIZATION_CODE
        )
        result = await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.AUTHORIZATION_CODE
        )
    finally:
        await client.aclose()

    assert result == {"access_token": "provider-token", "token_type": "Bearer"}
    assert len({request.client_address for request in oauth_server.requests}) == 1


@pytest.mark.anyio
async def test_httpx_fetcher_retries_concurrent_gateway_failures_on_fresh_connections(
    oauth_server: OAuthServer,
) -> None:
    release_failures = Event()
    oauth_server.expect("/token").respond(
        status=HTTPStatus.SERVICE_UNAVAILABLE,
        hold_until=release_failures,
    )
    oauth_server.expect("/token").respond(
        status=HTTPStatus.SERVICE_UNAVAILABLE,
        hold_until=release_failures,
    )
    for _ in range(2):
        oauth_server.expect("/token").respond(
            {"access_token": "provider-token", "token_type": "Bearer"}
        )
    client = oauth.create_httpx_upstream_client(
        FetchSettings(total_attempts=2, backoff_factor=0)
    )
    results: list[dict[str, object]] = []

    async def fetch() -> None:
        results.append(
            await client.fetch(
                oauth_server.url_for("/token"),
                types.UpstreamGrantType.AUTHORIZATION_CODE,
            )
        )

    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(fetch)
            task_group.start_soon(fetch)
            assert await anyio.to_thread.run_sync(oauth_server.wait_for_requests, 2, 1)
            release_failures.set()
    finally:
        await client.aclose()

    assert results == [
        {"access_token": "provider-token", "token_type": "Bearer"},
        {"access_token": "provider-token", "token_type": "Bearer"},
    ]
    initial_connections = {
        request.client_address for request in oauth_server.requests[:2]
    }
    retry_connections = {
        request.client_address for request in oauth_server.requests[2:]
    }
    assert initial_connections.isdisjoint(retry_connections)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "upstream_grant_type",
    [
        types.UpstreamGrantType.AUTHORIZATION_CODE,
        types.UpstreamGrantType.REFRESH_TOKEN,
    ],
)
async def test_httpx_fetcher_cancels_inflight_request_and_closes_within_deadline(
    upstream_grant_type: types.UpstreamGrantType,
) -> None:
    request_started = Event()
    release_request = Event()

    class OAuthHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        requests = 0

        def do_POST(self) -> None:  # noqa: N802 # Required by BaseHTTPRequestHandler.
            type(self).requests += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.requests == 1:
                request_started.set()
                assert release_request.wait(timeout=1)
            body = b'{"access_token":"provider-token","token_type":"Bearer"}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), OAuthHandler)
    server.daemon_threads = True
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    client = oauth.create_httpx_upstream_client(
        FetchSettings(total_attempts=2, backoff_factor=0)
    )
    cancelled = False

    async def fetch() -> None:
        nonlocal cancelled
        try:
            await client.fetch(f"http://{host}:{port}/token", upstream_grant_type)
        except anyio.get_cancelled_exc_class():
            cancelled = True

    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(fetch)
            assert await anyio.to_thread.run_sync(request_started.wait, 1)
            task_group.cancel_scope.cancel()

        release_request.set()
        result = await client.fetch(f"http://{host}:{port}/token", upstream_grant_type)
        with anyio.fail_after(1):
            await client.aclose()
    finally:
        release_request.set()
        server.shutdown()
        server.server_close()
        server_thread.join()

    assert cancelled is True
    assert result == {"access_token": "provider-token", "token_type": "Bearer"}
    assert OAuthHandler.requests == 2


@pytest.mark.anyio
async def test_httpx_upstream_client_shutdown_rejects_new_fetches() -> None:
    client = oauth.create_httpx_upstream_client(FetchSettings())
    await client.aclose()

    with pytest.raises(RuntimeError, match="shutting down"):
        await client.fetch(
            "https://provider.example.com/token",
            types.UpstreamGrantType.AUTHORIZATION_CODE,
        )
