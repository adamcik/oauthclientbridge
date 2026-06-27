import unittest.mock
from http import HTTPStatus

import flask.ctx
import pytest
import requests
from freezegun import freeze_time
from requests_mock import Mocker as RequestsMocker

from oauthclientbridge import oauth
from oauthclientbridge.settings import current_settings


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
        unittest.mock.patch("oauthclientbridge.oauth.time.time", side_effect=now),
        unittest.mock.patch("oauthclientbridge.oauth.time.monotonic", side_effect=now),
        unittest.mock.patch("oauthclientbridge.oauth.time.sleep", side_effect=sleep),
        unittest.mock.patch(
            "oauthclientbridge.oauth._fetch", side_effect=fetch_side_effect
        ),
    ):
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["error"] == "temporarily_unavailable"
    assert fake_time[0] == pytest.approx(0.2)


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
        unittest.mock.patch("oauthclientbridge.oauth.time.time", side_effect=now),
        unittest.mock.patch("oauthclientbridge.oauth.time.monotonic", side_effect=now),
        unittest.mock.patch("oauthclientbridge.oauth.time.sleep", side_effect=sleep),
        unittest.mock.patch(
            "oauthclientbridge.oauth._fetch", side_effect=fetch_side_effect
        ),
    ):
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    assert observed_timeouts[0] == pytest.approx(1.0)
    assert observed_timeouts[1] == pytest.approx(0.575)
    assert fake_time[0] == pytest.approx(1.0)


def test_oauth_fetch_jitters_retry_after_sleep(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 429, "headers": {"Retry-After": "10"}},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    with (
        unittest.mock.patch("random.uniform", return_value=0.75),
        unittest.mock.patch("time.sleep") as mock_sleep,
    ):
        oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    mock_sleep.assert_called_once_with(7.5)


def test_oauth_fetch_jitters_retry_backoff_within_bounds(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 503},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    with (
        unittest.mock.patch("random.uniform", return_value=1.25),
        unittest.mock.patch("time.sleep") as mock_sleep,
    ):
        oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    mock_sleep.assert_called_once_with(0.125)


def test_oauth_fetch_jitters_retry_after_sleeps_independently(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 429, "headers": {"Retry-After": "10"}},
            {"status_code": 429, "headers": {"Retry-After": "10"}},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    with (
        unittest.mock.patch("random.uniform", side_effect=[0.75, 1.25]),
        unittest.mock.patch("time.sleep") as mock_sleep,
    ):
        oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert mock_sleep.call_args_list[0].args[0] == 7.5
    assert mock_sleep.call_args_list[1].args[0] == 12.5
    assert mock_sleep.call_args_list[0].args[0] != mock_sleep.call_args_list[1].args[0]


def test_oauth_fetch_retries_on_failure_then_success(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    """Verify that oauth.fetch retries on HTTP failure and eventually succeeds."""
    # Simulate 2 failures (504 status code) then 1 success (200 status code)
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 504},
            {"status_code": 504},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    with unittest.mock.patch("time.sleep") as mock_sleep:
        oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")
        assert mock_sleep.call_count == 2


def test_oauth_fetch_retries_on_exception_then_success(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    """Verify that oauth.fetch retries on exception and eventually succeeds."""
    # Simulate 2 exceptions then 1 success
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"exc": requests.exceptions.ConnectionError("Test Connection Error")},
            {"exc": requests.exceptions.Timeout("Test Timeout Error")},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    with unittest.mock.patch("time.sleep") as mock_sleep:
        oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")
        assert mock_sleep.call_count == 2


def test_oauth_fetch_fails_after_all_retries_exhausted(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    """Verify that oauth.fetch fails after all retries are exhausted."""
    # Simulate 3 failures (504 status code) and total_retries is 2
    # This should result in 2 retries and then a final failure
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 504},
            {"status_code": 504},
            {"status_code": 504},
        ],
    )

    with unittest.mock.patch("time.sleep") as mock_sleep:
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")
        assert mock_sleep.call_count == 2

    assert "error" in result
    assert result["error"] == "temporarily_unavailable"


def test_oauth_fetch_respects_retry_after_header(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    """Verify that oauth.fetch respects the Retry-After header."""

    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 429, "headers": {"Retry-After": "10"}},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    with (
        unittest.mock.patch("random.uniform", return_value=0.75),
        unittest.mock.patch("time.sleep") as mock_sleep,
    ):
        oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")
        mock_sleep.assert_called_once_with(7.5)


def test_oauth_fetch_does_not_retry_on_non_retryable_status_code(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    """Verify that oauth.fetch does not retry on a non-retryable status code."""

    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 400},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    with unittest.mock.patch("time.sleep") as mock_sleep:
        oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")
        mock_sleep.assert_not_called()


def test_oauth_fetch_does_not_retry_on_success(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    """Verify that oauth.fetch does not retry on success."""

    requests_mock.post(
        current_settings.oauth.token_uri,
        json={"access_token": "mock_token", "token_type": "Bearer"},
        status_code=200,
    )

    with unittest.mock.patch("time.sleep") as mock_sleep:
        oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")
        mock_sleep.assert_not_called()


def test_oauth_fetch_closes_session_before_retrying_retryable_status(
    app_context: flask.ctx.AppContext,
) -> None:
    """Verify that retryable HTTP responses reset the pooled session."""

    first_response = unittest.mock.Mock(spec=requests.Response)
    first_response.json.return_value = {"error": "temporarily_unavailable"}
    first_response.status_code = 503
    first_response.content = b'{"error": "temporarily_unavailable"}'
    first_response.headers = {}

    second_response = unittest.mock.Mock(spec=requests.Response)
    second_response.json.return_value = {
        "access_token": "mock_token",
        "token_type": "Bearer",
    }
    second_response.status_code = 200
    second_response.content = b'{"access_token": "mock_token", "token_type": "Bearer"}'
    second_response.headers = {}

    session = unittest.mock.Mock(spec=requests.Session)
    session.send.side_effect = [first_response, second_response]

    with (
        unittest.mock.patch(
            "oauthclientbridge.oauth.get_session", return_value=session
        ),
        unittest.mock.patch("time.sleep"),
    ):
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    session.close.assert_called_once_with()


def test_parse_retry_with_seconds() -> None:
    assert oauth.parse_retry("10") == 10


@freeze_time("2025-01-01 00:00:00 UTC")
def test_parse_retry_with_http_date() -> None:
    # Now
    assert oauth.parse_retry("Wed, 01 Jan 2025 00:00:00 GMT") == 0
    # Future date
    assert oauth.parse_retry("Fri, 01 Jan 2025 00:00:10 GMT") == 10
    assert oauth.parse_retry("Fri, 01 Jan 2025 00:00:30 CET") == 30
    assert oauth.parse_retry("Fri, 01 Jan 2025 00:00:00 PST") == 28800
    # Past date
    assert oauth.parse_retry("Fri, 01 Jan 2024 00:00:00 GMT") == 0
    assert oauth.parse_retry("Fri, 01 Jan 2024 00:00:00 CET") == 0
    assert oauth.parse_retry("Fri, 01 Jan 2024 00:00:00 PST") == 0


def test_parse_retry_with_none() -> None:
    assert oauth.parse_retry(None) == 0


def test_parse_retry_with_invalid_string() -> None:
    assert oauth.parse_retry("invalid") == 0
    assert oauth.parse_retry("numb3r") == 0
    assert oauth.parse_retry("0x15") == 0


def test_parse_retry_with_multiple_headers() -> None:
    assert oauth.parse_retry("10, 20") == 0
    assert oauth.parse_retry("10, Wed, 01 Jan 2025 00:00:10 GMT") == 0


def test_oauth_session_sets_user_agent(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    """Verify that oauth.session sets the User-Agent header correctly."""

    requests_mock.get("http://example.com/", status_code=200)

    oauth.get_session().get("http://example.com/")

    history = requests_mock.request_history
    assert len(history) == 1
    assert "User-Agent" in history[0].headers
    assert history[0].headers["User-Agent"].startswith("oauthclientbridge")


# TODO: test that fetch also uses this session.
