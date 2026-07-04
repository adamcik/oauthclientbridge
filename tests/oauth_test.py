import unittest.mock

import flask.ctx
import requests
from freezegun import freeze_time
from requests_mock import Mocker as RequestsMocker

from oauthclientbridge import oauth
from oauthclientbridge.oauth.core import parse_retry
from oauthclientbridge.oauth import core as oauth_core
from oauthclientbridge.settings import current_settings


def test_oauth_fetch_does_not_call_requests_with_expired_deadline(
    app_context: flask.ctx.AppContext,
) -> None:
    current_settings.fetch.total_timeout = 0.0
    current_settings.fetch.total_retries = 1

    with unittest.mock.patch(
        "oauthclientbridge.oauth.core.get_session"
    ) as mock_get_session:
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["error"] == "server_error"
    assert mock_get_session.call_count == 0


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

    mock_sleep.assert_called_once_with(10)


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


def test_oauth_fetch_uses_configured_jitter_bounds(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    current_settings.fetch.backoff_jitter_min = 1.0
    current_settings.fetch.backoff_jitter_max = 2.0

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
        unittest.mock.patch("random.uniform", side_effect=lambda low, high: high),
        unittest.mock.patch("time.sleep") as mock_sleep,
    ):
        oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    mock_sleep.assert_called_once_with(0.2)


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

    assert mock_sleep.call_args_list[0].args[0] == 10
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
    current_settings.fetch.total_retries = 2
    # Simulate 3 failures (504 status code): 1 initial attempt + 2 retries.
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
        mock_sleep.assert_called_once_with(10)


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


def test_oauth_fetch_does_not_retry_on_500_status_code(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 500},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    with unittest.mock.patch("time.sleep") as mock_sleep:
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    mock_sleep.assert_not_called()
    assert result["error"] == "server_error"


def test_oauth_fetch_retries_on_502_status_code(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    requests_mock.post(
        current_settings.oauth.token_uri,
        [
            {"status_code": 502},
            {
                "json": {"access_token": "mock_token", "token_type": "Bearer"},
                "status_code": 200,
            },
        ],
    )

    with unittest.mock.patch("time.sleep") as mock_sleep:
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    mock_sleep.assert_called_once()
    assert result["access_token"] == "mock_token"


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
            "oauthclientbridge.oauth.core.get_session", return_value=session
        ),
        unittest.mock.patch("time.sleep"),
    ):
        result = oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")

    assert result["access_token"] == "mock_token"
    session.close.assert_called_once_with()


def test_parse_retry_with_seconds() -> None:
    assert parse_retry("10") == 10


@freeze_time("2025-01-01 00:00:00 UTC")
def test_parse_retry_with_http_date() -> None:
    # Now
    assert parse_retry("Wed, 01 Jan 2025 00:00:00 GMT") == 0
    # Future date
    assert parse_retry("Fri, 01 Jan 2025 00:00:10 GMT") == 10
    assert parse_retry("Fri, 01 Jan 2025 00:00:30 CET") == 30
    assert parse_retry("Fri, 01 Jan 2025 00:00:00 PST") == 28800
    # Past date
    assert parse_retry("Fri, 01 Jan 2024 00:00:00 GMT") == 0
    assert parse_retry("Fri, 01 Jan 2024 00:00:00 CET") == 0
    assert parse_retry("Fri, 01 Jan 2024 00:00:00 PST") == 0


def test_parse_retry_with_none() -> None:
    assert parse_retry(None) == 0


def test_parse_retry_with_invalid_string() -> None:
    assert parse_retry("invalid") == 0
    assert parse_retry("numb3r") == 0
    assert parse_retry("0x15") == 0


def test_parse_retry_with_multiple_headers() -> None:
    assert parse_retry("10, 20") == 0
    assert parse_retry("10, Wed, 01 Jan 2025 00:00:10 GMT") == 0


def test_oauth_session_sets_user_agent(
    app_context: flask.ctx.AppContext,
    requests_mock: RequestsMocker,
) -> None:
    """Verify that oauth.session sets the User-Agent header correctly."""

    requests_mock.get("http://example.com/", status_code=200)

    oauth_core.get_session().get("http://example.com/")

    history = requests_mock.request_history
    assert len(history) == 1
    assert "User-Agent" in history[0].headers
    assert history[0].headers["User-Agent"].startswith("oauthclientbridge")


# TODO: test that fetch also uses this session.
