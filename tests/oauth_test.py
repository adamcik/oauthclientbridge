import unittest.mock

import flask.ctx
import requests
from requests_mock import Mocker

from oauthclientbridge import oauth
from oauthclientbridge.settings import current_settings


def test_oauth_fetch_retries_on_failure_then_success(
    app_context: flask.ctx.AppContext,
    requests_mock: Mocker,
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
    requests_mock: Mocker,
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
    requests_mock: Mocker,
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
    requests_mock: Mocker,
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

    with unittest.mock.patch("time.sleep") as mock_sleep:
        oauth.fetch(current_settings.oauth.token_uri, "test_endpoint")
        mock_sleep.assert_called_once_with(10)


def test_oauth_fetch_does_not_retry_on_non_retryable_status_code(
    app_context: flask.ctx.AppContext,
    requests_mock: Mocker,
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
    requests_mock: Mocker,
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
