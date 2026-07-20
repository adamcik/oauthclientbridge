import unittest.mock
from dataclasses import dataclass
from http import HTTPStatus

import flask.ctx
import pytest
import requests
from opentelemetry import trace
from requests_mock import Mocker as RequestsMocker

from oauthclientbridge import oauth
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.oauth import (
    _core as oauth_core,  # pyright: ignore[reportPrivateUsage] # Direct implementation test.
)
from oauthclientbridge.oauth import (
    _retry as oauth_retry,  # pyright: ignore[reportPrivateUsage] # Direct implementation test.
)
from oauthclientbridge.oauth._outcome import (
    OAuthResponse,  # pyright: ignore[reportPrivateUsage] # Direct implementation test.
)
from oauthclientbridge.settings import current_settings


@dataclass
class MockTime:
    wall_seconds: float = 0.0
    monotonic_seconds: float = 0.0

    def time(self) -> float:
        return self.wall_seconds

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def sleep(self, seconds: float) -> None:
        self.advance(monotonic=seconds)

    def advance(self, *, monotonic: float, wall: float | None = None) -> None:
        self.monotonic_seconds += monotonic
        self.wall_seconds += monotonic if wall is None else wall


@pytest.fixture
def mock_time(monkeypatch: pytest.MonkeyPatch) -> MockTime:
    clock = MockTime()
    monkeypatch.setattr(oauth_core.time, "time", clock.time)
    monkeypatch.setattr(oauth_core.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(oauth_core.time, "sleep", clock.sleep)
    return clock


def test_retry_limiter_factory_is_cached(app_context: flask.ctx.AppContext) -> None:
    oauth_retry.get_retry_limiter.cache_clear()
    current_settings.fetch.retry_budget_capacity = 8
    current_settings.fetch.retry_budget_refill_per_initial = 0.5

    limiter1 = oauth_retry.get_retry_limiter(8, 0.5)
    limiter2 = oauth_retry.get_retry_limiter(8, 0.5)

    assert limiter1 is limiter2
    assert limiter1.capacity == 8
    assert limiter1.refill_amount == 0.5


def test_retry_limiter_factory_refreshes_when_settings_change() -> None:
    oauth_retry.get_retry_limiter.cache_clear()
    limiter1 = oauth_retry.get_retry_limiter(8, 0.5)
    limiter2 = oauth_retry.get_retry_limiter(3, 1.0)

    assert limiter2 is not limiter1
    assert limiter2.capacity == 3
    assert limiter2.refill_amount == 1.0


def test_oauth_fetch_skips_retry_when_retry_budget_exhausted(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
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
            self.consume_calls = getattr(self, "consume_calls", []) + [tokens]
            return False

    fake_limiter = FakeRetryLimiter()

    with (
        unittest.mock.patch.object(
            oauth_core, "_get_retry_limiter", return_value=fake_limiter
        ),
        unittest.mock.patch("time.sleep") as mock_sleep,
    ):
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    mock_sleep.assert_not_called()
    assert result["error"] == "temporarily_unavailable"
    assert getattr(fake_limiter, "add_calls", []) == [0.25]
    assert getattr(fake_limiter, "consume_calls", []) == [1]


def test_oauth_fetch_still_runs_first_attempt_when_retry_budget_exhausted(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"access_token": "mock_token", "token_type": "Bearer"},
        status_code=200,
    )

    class FakeRetryLimiter:
        def add(self, tokens: float) -> None:
            self.add_calls = getattr(self, "add_calls", []) + [tokens]

        def consume(self, tokens: float = 1) -> bool:
            self.consume_calls = getattr(self, "consume_calls", []) + [tokens]
            return False

    fake_limiter = FakeRetryLimiter()

    with unittest.mock.patch.object(
        oauth_core, "_get_retry_limiter", return_value=fake_limiter
    ):
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    assert getattr(fake_limiter, "add_calls", []) == [0.25]
    assert getattr(fake_limiter, "consume_calls", []) == []


def test_oauth_fetch_retries_when_retry_budget_is_available(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
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
            self.consume_calls = getattr(self, "consume_calls", []) + [tokens]
            return True

    fake_limiter = FakeRetryLimiter()

    with unittest.mock.patch.object(
        oauth_core, "_get_retry_limiter", return_value=fake_limiter
    ):
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    assert getattr(fake_limiter, "add_calls", []) == [0.25]
    assert getattr(fake_limiter, "consume_calls", []) == [1]


def test_oauth_fetch_normalizes_retryable_invalid_client_to_temporarily_unavailable(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    current_settings.fetch.total_retries = 0
    requests_mock.post(
        current_settings.oauth.token_uri,
        status_code=503,
        json={"error": OAuthError.INVALID_CLIENT},
    )

    result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["error"] == OAuthError.TEMPORARILY_UNAVAILABLE


def test_oauth_fetch_still_runs_initial_attempt_when_total_retries_is_zero(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    current_settings.fetch.total_retries = 0
    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"access_token": "mock_token", "token_type": "Bearer"},
        status_code=200,
    )

    result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    assert len(requests_mock.request_history) == 1


def test_oauth_fetch_total_retries_allows_one_retry(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    current_settings.fetch.total_retries = 1
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
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    assert len(requests_mock.request_history) == 2


def test_oauth_fetch_does_not_start_retry_after_sleep_exhausts_deadline(
    app_context: flask.ctx.AppContext,
    monkeypatch: pytest.MonkeyPatch,
    mock_time: MockTime,
) -> None:
    current_settings.fetch.total_timeout = 1.0
    current_settings.fetch.total_retries = 2
    current_settings.fetch.backoff_factor = 0.8

    first_result = (
        {"error": "temporarily_unavailable"},
        HTTPStatus.SERVICE_UNAVAILABLE,
        0,
    )

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
            mock_time.advance(monotonic=0.2)
            return first_result

        raise AssertionError("unexpected retry attempt")

    monkeypatch.setattr(oauth_core.random, "uniform", lambda _low, _high: 1.25)
    monkeypatch.setattr(oauth_core, "_fetch", fetch_side_effect)

    result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["error"] == "temporarily_unavailable"
    assert mock_time.monotonic_seconds == pytest.approx(0.2)


def test_oauth_fetch_total_deadline_uses_monotonic_clock(
    app_context: flask.ctx.AppContext,
    monkeypatch: pytest.MonkeyPatch,
    mock_time: MockTime,
) -> None:
    current_settings.fetch.total_timeout = 1.0
    current_settings.fetch.total_retries = 1
    current_settings.fetch.backoff_factor = 0.3

    observed_timeouts: list[float] = []
    mock_time.advance(monotonic=0.0, wall=100.0)

    def fetch_side_effect(
        span: trace.Span,
        prepared: requests.PreparedRequest,
        timeout: float,
        endpoint: str,
    ) -> tuple[OAuthResponse, HTTPStatus | None, int]:
        _ = span, prepared, endpoint
        observed_timeouts.append(timeout)
        if len(observed_timeouts) == 1:
            mock_time.advance(monotonic=0.2, wall=500.0)
            return (
                {"error": "temporarily_unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                0,
            )

        return (
            {"access_token": "mock_token", "token_type": "Bearer"},
            HTTPStatus.OK,
            0,
        )

    monkeypatch.setattr(oauth_core.random, "uniform", lambda _low, _high: 0.75)
    monkeypatch.setattr(oauth_core, "_fetch", fetch_side_effect)

    result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    assert observed_timeouts == pytest.approx([1.0, 0.575])


def test_oauth_fetch_uses_remaining_budget_for_retry_timeout(
    app_context: flask.ctx.AppContext,
    monkeypatch: pytest.MonkeyPatch,
    mock_time: MockTime,
) -> None:
    current_settings.fetch.total_timeout = 1.0
    current_settings.fetch.total_retries = 2
    current_settings.fetch.backoff_factor = 0.3

    observed_timeouts: list[float] = []

    def fetch_side_effect(
        span: trace.Span,
        prepared: requests.PreparedRequest,
        timeout: float,
        endpoint: str,
    ) -> tuple[OAuthResponse, HTTPStatus | None, int]:
        _ = span, prepared, endpoint
        observed_timeouts.append(timeout)
        if len(observed_timeouts) == 1:
            mock_time.advance(monotonic=0.2)
            return (
                {"error": "temporarily_unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                0,
            )

        mock_time.advance(monotonic=timeout)
        return (
            {"access_token": "mock_token", "token_type": "Bearer"},
            HTTPStatus.OK,
            0,
        )

    monkeypatch.setattr(oauth_core.random, "uniform", lambda _low, _high: 0.75)
    monkeypatch.setattr(oauth_core, "_fetch", fetch_side_effect)

    result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    assert observed_timeouts[0] == pytest.approx(1.0)
    assert observed_timeouts[1] == pytest.approx(0.575)
