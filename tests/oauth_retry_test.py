import unittest.mock
from http import HTTPStatus

import flask.ctx
import pytest
from requests_mock import Mocker as RequestsMocker

from oauthclientbridge import oauth
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.oauth import core as oauth_core
from oauthclientbridge.oauth import retry as oauth_retry
from oauthclientbridge.settings import current_settings


def test_retry_limiter_consumes_token_on_admission() -> None:
    limiter = oauth_retry.RetryLimiter(capacity=1, refill_per_initial=0.25)

    assert limiter.allow_retry() is True
    assert limiter.allow_retry() is False


def test_retry_limiter_refills_from_initial_usage() -> None:
    limiter = oauth_retry.RetryLimiter(capacity=1, refill_per_initial=0.25)

    assert limiter.allow_retry() is True
    limiter.record_initial()
    limiter.record_initial()
    limiter.record_initial()
    assert limiter.allow_retry() is False

    limiter.record_initial()
    assert limiter.allow_retry() is True


def test_retry_limiter_factory_is_cached(app_context: flask.ctx.AppContext) -> None:
    oauth_retry.get_retry_limiter.cache_clear()
    current_settings.fetch.retry_budget_capacity = 8
    current_settings.fetch.retry_budget_refill_per_initial = 0.5

    limiter1 = oauth_retry.get_retry_limiter(8, 0.5)
    limiter2 = oauth_retry.get_retry_limiter(8, 0.5)

    assert limiter1 is limiter2
    assert limiter1.capacity == 8
    assert limiter1.refill_per_initial == 0.5


def test_retry_limiter_factory_refreshes_when_settings_change() -> None:
    oauth_retry.get_retry_limiter.cache_clear()
    limiter1 = oauth_retry.get_retry_limiter(8, 0.5)
    limiter2 = oauth_retry.get_retry_limiter(3, 1.0)

    assert limiter2 is not limiter1
    assert limiter2.capacity == 3
    assert limiter2.refill_per_initial == 1.0


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
        def record_initial(self) -> None:
            self.initial_calls = getattr(self, "initial_calls", 0) + 1

        def allow_retry(self) -> bool:
            self.allow_calls = getattr(self, "allow_calls", 0) + 1
            return False

        def record_retry(self) -> None:
            self.retry_calls = getattr(self, "retry_calls", 0) + 1

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
    assert getattr(fake_limiter, "initial_calls", 0) == 1
    assert getattr(fake_limiter, "allow_calls", 0) == 1
    assert getattr(fake_limiter, "retry_calls", 0) == 0


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
        def record_initial(self) -> None:
            self.initial_calls = getattr(self, "initial_calls", 0) + 1

        def allow_retry(self) -> bool:
            self.allow_calls = getattr(self, "allow_calls", 0) + 1
            return False

        def record_retry(self) -> None:
            self.retry_calls = getattr(self, "retry_calls", 0) + 1

    fake_limiter = FakeRetryLimiter()

    with unittest.mock.patch.object(
        oauth_core, "_get_retry_limiter", return_value=fake_limiter
    ):
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    assert getattr(fake_limiter, "initial_calls", 0) == 1
    assert getattr(fake_limiter, "allow_calls", 0) == 0
    assert getattr(fake_limiter, "retry_calls", 0) == 0


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
        def record_initial(self) -> None:
            self.initial_calls = getattr(self, "initial_calls", 0) + 1

        def allow_retry(self) -> bool:
            self.allow_calls = getattr(self, "allow_calls", 0) + 1
            return True

        def record_retry(self) -> None:
            self.retry_calls = getattr(self, "retry_calls", 0) + 1

    fake_limiter = FakeRetryLimiter()

    with unittest.mock.patch.object(
        oauth_core, "_get_retry_limiter", return_value=fake_limiter
    ):
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    assert getattr(fake_limiter, "initial_calls", 0) == 1
    assert getattr(fake_limiter, "allow_calls", 0) == 1
    assert getattr(fake_limiter, "retry_calls", 0) == 1


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
) -> None:
    current_settings.fetch.total_timeout = 1.0
    current_settings.fetch.total_retries = 2
    current_settings.fetch.backoff_factor = 0.8

    fake_time = [0.0]

    def now() -> float:
        return fake_time[0]

    def sleep(duration: float) -> None:
        fake_time[0] += duration

    first_result = (
        {"error": "temporarily_unavailable"},
        HTTPStatus.SERVICE_UNAVAILABLE,
        0,
    )

    def fetch_side_effect(*args, **kwargs):
        if fetch_side_effect.call_count == 0:
            fetch_side_effect.call_count += 1
            fake_time[0] += 0.2
            return first_result

        raise AssertionError("unexpected retry attempt")

    fetch_side_effect.call_count = 0

    with (
        unittest.mock.patch("random.uniform", return_value=1.25),
        unittest.mock.patch("oauthclientbridge.oauth.core.time.time", side_effect=now),
        unittest.mock.patch(
            "oauthclientbridge.oauth.core.time.monotonic", side_effect=now
        ),
        unittest.mock.patch(
            "oauthclientbridge.oauth.core.time.sleep", side_effect=sleep
        ),
        unittest.mock.patch(
            "oauthclientbridge.oauth.core._fetch", side_effect=fetch_side_effect
        ),
    ):
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["error"] == "temporarily_unavailable"
    assert fake_time[0] == pytest.approx(0.2)


def test_oauth_fetch_total_deadline_uses_monotonic_clock(
    app_context: flask.ctx.AppContext,
) -> None:
    current_settings.fetch.total_timeout = 1.0
    current_settings.fetch.total_retries = 1
    current_settings.fetch.backoff_factor = 0.3

    monotonic_time = [0.0]
    wall_time = [100.0]
    observed_timeouts: list[float] = []

    def monotonic_now() -> float:
        return monotonic_time[0]

    def wall_now() -> float:
        return wall_time[0]

    def sleep(duration: float) -> None:
        monotonic_time[0] += duration
        wall_time[0] += 50.0

    def fetch_side_effect(*args, **kwargs):
        timeout = args[2]
        observed_timeouts.append(timeout)
        if len(observed_timeouts) == 1:
            monotonic_time[0] += 0.2
            wall_time[0] += 500.0
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

    with (
        unittest.mock.patch("random.uniform", return_value=0.75),
        unittest.mock.patch(
            "oauthclientbridge.oauth.core.time.time", side_effect=wall_now
        ),
        unittest.mock.patch(
            "oauthclientbridge.oauth.core.time.monotonic", side_effect=monotonic_now
        ),
        unittest.mock.patch(
            "oauthclientbridge.oauth.core.time.sleep", side_effect=sleep
        ),
        unittest.mock.patch(
            "oauthclientbridge.oauth.core._fetch", side_effect=fetch_side_effect
        ),
    ):
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    assert observed_timeouts == pytest.approx([1.0, 0.575])


def test_oauth_fetch_uses_remaining_budget_for_retry_timeout(
    app_context: flask.ctx.AppContext,
) -> None:
    current_settings.fetch.total_timeout = 1.0
    current_settings.fetch.total_retries = 2
    current_settings.fetch.backoff_factor = 0.3

    fake_time = [0.0]
    observed_timeouts: list[float] = []

    def now() -> float:
        return fake_time[0]

    def sleep(duration: float) -> None:
        fake_time[0] += duration

    def fetch_side_effect(*args, **kwargs):
        timeout = args[2]
        observed_timeouts.append(timeout)
        if len(observed_timeouts) == 1:
            fake_time[0] += 0.2
            return (
                {"error": "temporarily_unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                0,
            )

        fake_time[0] += timeout
        return (
            {"access_token": "mock_token", "token_type": "Bearer"},
            HTTPStatus.OK,
            0,
        )

    with (
        unittest.mock.patch("random.uniform", return_value=0.75),
        unittest.mock.patch("oauthclientbridge.oauth.core.time.time", side_effect=now),
        unittest.mock.patch(
            "oauthclientbridge.oauth.core.time.monotonic", side_effect=now
        ),
        unittest.mock.patch(
            "oauthclientbridge.oauth.core.time.sleep", side_effect=sleep
        ),
        unittest.mock.patch(
            "oauthclientbridge.oauth.core._fetch", side_effect=fetch_side_effect
        ),
    ):
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    assert observed_timeouts[0] == pytest.approx(1.0)
    assert observed_timeouts[1] == pytest.approx(0.575)
