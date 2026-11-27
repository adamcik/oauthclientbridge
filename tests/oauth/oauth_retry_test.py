import time
from dataclasses import dataclass
from unittest import mock

import anyio
import pytest

from oauthclientbridge import types
from oauthclientbridge.oauth import (
    _core,  # pyright: ignore[reportPrivateUsage] # Control jitter sampling.
    _httpx,  # pyright: ignore[reportPrivateUsage] # Control transport and retry delay.
    _retry,  # pyright: ignore[reportPrivateUsage] # Direct limiter contract test.
)
from oauthclientbridge.settings import FetchSettings
from tests.oauth_server import OAuthServer


def test_retry_limiter_factory_is_cached() -> None:
    _retry.get_retry_limiter.cache_clear()

    limiter1 = _retry.get_retry_limiter(8, 0.5)
    limiter2 = _retry.get_retry_limiter(8, 0.5)

    assert limiter1 is limiter2
    assert limiter1.capacity == 8
    assert limiter1.refill_amount == 0.5


def test_retry_limiter_factory_refreshes_when_settings_change() -> None:
    _retry.get_retry_limiter.cache_clear()
    limiter1 = _retry.get_retry_limiter(8, 0.5)
    limiter2 = _retry.get_retry_limiter(3, 1.0)

    assert limiter2 is not limiter1
    assert limiter2.capacity == 3
    assert limiter2.refill_amount == 1.0


@dataclass(frozen=True)
class RetryCase:
    id: str
    statuses: tuple[int, ...]
    attempts: int
    expected_error: str | None = None
    provider_error: str | None = "invalid_client"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        RetryCase(id="initial-only", statuses=(200,), attempts=1),
        RetryCase(id="success-no-retry", statuses=(200,), attempts=3),
        RetryCase(id="one-retry", statuses=(503, 200), attempts=2),
        RetryCase(id="gateway-retry", statuses=(502, 200), attempts=2),
        RetryCase(id="two-retries", statuses=(504, 504, 200), attempts=3),
        RetryCase(
            id="exhausted",
            statuses=(504, 504, 504),
            attempts=3,
            expected_error="temporarily_unavailable",
        ),
        RetryCase(
            id="invalid-client-unavailable",
            statuses=(503,),
            attempts=1,
            expected_error="temporarily_unavailable",
        ),
        RetryCase(
            id="bad-request-terminal",
            statuses=(400,),
            attempts=3,
            expected_error="server_error",
            provider_error=None,
        ),
        RetryCase(
            id="server-error-terminal",
            statuses=(500,),
            attempts=3,
            expected_error="server_error",
            provider_error=None,
        ),
    ],
    ids=lambda case: case.id,
)
async def test_httpx_retry_policy(case: RetryCase, oauth_server: OAuthServer) -> None:
    for status in case.statuses:
        oauth_server.expect("/token").respond(
            {"access_token": "token", "token_type": "Bearer"}
            if status == 200
            else {"error": case.provider_error}
            if case.provider_error
            else None,
            status=status,
        )
    async with oauth_server.client(
        FetchSettings(total_attempts=case.attempts, backoff_factor=0)
    ) as client:
        result = await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.REFRESH_TOKEN
        )
    assert len(oauth_server.requests) == len(case.statuses)
    assert (
        oauth_server.requests[0].headers["User-Agent"].startswith("oauthclientbridge ")
    )
    if case.expected_error is None:
        assert result["access_token"] == "token"
    else:
        assert result["error"] == case.expected_error


@dataclass(frozen=True)
class JitterCase:
    id: str
    retry_after: str | None
    samples: tuple[float, ...]
    expected_delays: tuple[float, ...]
    lower: float = 0.75
    upper: float = 1.25


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        JitterCase(
            id="backoff", retry_after=None, samples=(1.25,), expected_delays=(0.125,)
        ),
        JitterCase(
            id="configured-bounds",
            retry_after=None,
            samples=(2.0,),
            expected_delays=(0.2,),
            lower=1,
            upper=2,
        ),
        JitterCase(
            id="provider-floor",
            retry_after="10",
            samples=(0.75,),
            expected_delays=(10,),
        ),
        JitterCase(
            id="independent-samples",
            retry_after="10",
            samples=(0.75, 1.25),
            expected_delays=(10, 12.5),
        ),
    ],
    ids=lambda case: case.id,
)
async def test_httpx_retry_jitter(
    case: JitterCase, oauth_server: OAuthServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in case.samples:
        oauth_server.expect("/token").respond(
            status=429 if case.retry_after else 503,
            headers={"Retry-After": case.retry_after} if case.retry_after else {},
        )
    oauth_server.expect("/token").respond(
        {"access_token": "token", "token_type": "Bearer"}
    )
    uniform = mock.Mock(side_effect=case.samples)
    sleep = mock.AsyncMock()
    monkeypatch.setattr(_core.random, "uniform", uniform)
    monkeypatch.setattr(_httpx, "_sleep", sleep)
    async with oauth_server.client(
        FetchSettings(backoff_jitter_min=case.lower, backoff_jitter_max=case.upper)
    ) as client:
        result = await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.REFRESH_TOKEN
        )
    assert result["access_token"] == "token"
    assert [call.args[0] for call in sleep.await_args_list] == list(
        case.expected_delays
    )
    assert uniform.call_args_list == [
        mock.call(max(1, case.lower) if case.retry_after else case.lower, case.upper)
    ] * len(case.samples)


@pytest.mark.anyio
async def test_httpx_expired_deadline_sends_no_request(
    oauth_server: OAuthServer,
) -> None:
    async with oauth_server.client(FetchSettings(total_timeout=0)) as client:
        result = await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.REFRESH_TOKEN
        )
    assert result["error"] == "server_error"
    assert not oauth_server.requests


@pytest.mark.anyio
async def test_httpx_retries_share_monotonic_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
    oauth_server: OAuthServer,
) -> None:
    deadlines: list[float] = []
    remaining: list[float] = []

    async def observe_delay(_: float) -> None:
        deadlines.append(anyio.current_effective_deadline())
        remaining.append(deadlines[-1] - anyio.current_time())
        if len(deadlines) == 1:
            monkeypatch.setattr(time, "time", lambda: 2_000_000_000)

    monkeypatch.setattr(_httpx, "_sleep", observe_delay)
    oauth_server.expect("/token").respond(status=503)
    oauth_server.expect("/token").respond(status=503)
    oauth_server.expect("/token").respond(
        {"access_token": "token", "token_type": "Bearer"}
    )
    async with oauth_server.client(
        FetchSettings(total_timeout=5, backoff_factor=0.1)
    ) as client:
        result = await client.fetch(
            oauth_server.url_for("/token"), types.UpstreamGrantType.REFRESH_TOKEN
        )
    assert result["access_token"] == "token"
    assert len(oauth_server.requests) == 3
    assert deadlines[0] == deadlines[1]
    assert all(0 < budget <= 5 for budget in remaining)
