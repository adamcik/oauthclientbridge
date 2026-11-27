"""OAuth fetch contracts over the prepared HTTPS client."""

from dataclasses import dataclass
from http import HTTPStatus

import anyio
import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import HistogramDataPoint, NumberDataPoint
from pytest_otel_capture import OTelMocker, assert_trace_header, latest_metric_data

from oauthclientbridge import telemetry, types
from oauthclientbridge.oauth import _httpx  # pyright: ignore[reportPrivateUsage] # Direct tracker and retry-delay tests.
from oauthclientbridge.settings import FetchSettings, PrometheusSettings
from tests.oauth_server import OAuthServer


@pytest.mark.anyio
async def test_fetch_preserves_upstream_telemetry(
    oauth_server: OAuthServer,
    otel_mock: OTelMocker,
    instrumented: None,
) -> None:
    oauth_server.expect("/token").respond(
        {"access_token": "provider-token", "token_type": "Bearer"}
    )
    async with oauth_server.client() as client:
        with trace.get_tracer("tests").start_as_current_span("fetch upstream token"):
            result = await client.fetch(
                oauth_server.url_for("/token"),
                types.UpstreamGrantType.AUTHORIZATION_CODE,
            )
    assert result == {"access_token": "provider-token", "token_type": "Bearer"}
    spans = otel_mock.get_finished_spans()
    parent = next(span for span in spans if span.name == "fetch upstream token")
    assert_trace_header(
        oauth_server.requests[0].headers["traceparent"], parent.trace_id
    )
    attributes = {"operation": "authorization_code", "final.result": "success"}
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
    assert total.value == duration.count == 1
    assert retries.sum == 0
    requests = [span for span in spans if span.name == "POST"]
    assert len(requests) == 1
    assert requests[0].attributes["httpx.connection.setup"] is False


@pytest.mark.anyio
async def test_fetch_tracker_cancels_remaining_work_after_deadline() -> None:
    fetches = _httpx._FetchTracker()  # pyright: ignore[reportPrivateUsage] # Direct lifecycle contract.
    started, cancelled = anyio.Event(), anyio.Event()

    async def fetch() -> None:
        try:
            async with fetches.track():
                started.set()
                await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            cancelled.set()

    async with anyio.create_task_group() as group:
        group.start_soon(fetch)
        await started.wait()
        await fetches.aclose(0)
        await cancelled.wait()
        with pytest.raises(RuntimeError, match="shutting down"):
            async with fetches.track():
                pass


@pytest.mark.anyio
async def test_fetch_tracker_drains_work_completed_before_deadline() -> None:
    fetches = _httpx._FetchTracker()  # pyright: ignore[reportPrivateUsage] # Direct lifecycle contract.
    started, release, closed = anyio.Event(), anyio.Event(), anyio.Event()

    async def fetch() -> None:
        async with fetches.track():
            started.set()
            await release.wait()

    async def close() -> None:
        await fetches.aclose(5)
        closed.set()

    async with anyio.create_task_group() as group:
        group.start_soon(fetch)
        await started.wait()
        group.start_soon(close)
        await fetches._closing_started.wait()  # pyright: ignore[reportPrivateUsage] # Lifecycle synchronization.
        assert not closed.is_set()
        with pytest.raises(RuntimeError, match="shutting down"):
            async with fetches.track():
                pass
        release.set()
        await closed.wait()


@dataclass(frozen=True)
class RetrySuppressionCase:
    id: str
    settings: FetchSettings
    reason: str
    retry_after: int


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        RetrySuppressionCase(
            id="budget-exhausted",
            settings=FetchSettings(
                total_attempts=2, backoff_factor=0, retry_budget_capacity=0
            ),
            reason="budget_exhausted",
            retry_after=1,
        ),
        RetrySuppressionCase(
            id="delay-exceeds-deadline",
            settings=FetchSettings(
                total_attempts=2, total_timeout=20, backoff_factor=0
            ),
            reason="deadline_exceeded",
            retry_after=21,
        ),
    ],
    ids=lambda case: case.id,
)
async def test_suppressed_retry_keeps_the_connection(
    case: RetrySuppressionCase,
    oauth_server: OAuthServer,
    otel_mock: OTelMocker,
) -> None:
    oauth_server.expect("/token").respond(
        {}, status=503, headers={"Retry-After": str(case.retry_after)}
    )
    oauth_server.expect("/token").respond(
        {"access_token": "next", "token_type": "Bearer"}
    )
    async with oauth_server.client(case.settings) as client:
        with trace.get_tracer("tests").start_as_current_span("fetch upstream token"):
            result = await client.fetch(
                oauth_server.url_for("/token"), types.UpstreamGrantType.REFRESH_TOKEN
            )
        assert len(oauth_server.requests) == 1
        assert result["error"] == "temporarily_unavailable"
        assert result["retry_after"] == case.retry_after
        await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.REFRESH_TOKEN
        )
    assert len({request.client_address for request in oauth_server.requests}) == 1
    events = [
        event
        for span in otel_mock.get_finished_spans()
        for event in span.events
        if event.name == "Retry suppressed"
    ]
    assert len(events) == 1
    assert events[0].attributes == {
        "oauth.upstream_grant_type": "refresh_token",
        "retry.reason": case.reason,
    }
    assert (b'reason="' + case.reason.encode() + b'"} 1.0') in telemetry.export_metrics(
        PrometheusSettings()
    )


@pytest.mark.anyio
async def test_cancelled_retry_delay_preserves_provider_failure(
    oauth_server: OAuthServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancel_delay(_: float) -> None:
        raise TimeoutError

    monkeypatch.setattr(_httpx, "_sleep", cancel_delay)
    oauth_server.expect("/token").respond(status=503, headers={"Retry-After": "1"})
    async with oauth_server.client(FetchSettings(total_attempts=2)) as client:
        result = await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.AUTHORIZATION_CODE
        )
    assert result == {
        "error": "temporarily_unavailable",
        "error_description": "Provider is unavailable.",
        "retry_after": 1,
    }
    assert len(oauth_server.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status", [HTTPStatus.BAD_REQUEST, HTTPStatus.FOUND])
async def test_fetch_rejects_non_success_token_payload(
    status: HTTPStatus,
    oauth_server: OAuthServer,
) -> None:
    oauth_server.expect("/token").respond(
        {"access_token": "must-not-escape", "token_type": "Bearer"},
        status=status,
        headers={"Location": "https://unconfigured.invalid/token"},
    )
    async with oauth_server.client() as client:
        result = await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.AUTHORIZATION_CODE
        )
    assert result["error"] == "server_error"
    assert "access_token" not in result
    assert len(oauth_server.requests) == 1
