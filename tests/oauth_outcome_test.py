from http import HTTPStatus

import flask.ctx
import pytest

from oauthclientbridge.errors import OAuthError
from oauthclientbridge.oauth import outcome as oauth_outcome
from oauthclientbridge.oauth.core import Error, error_handler
from oauthclientbridge.oauth.retry import RetryReason
from oauthclientbridge.settings import current_settings


def test_error_handler_returns_503_for_temporarily_unavailable_retry_after(
    app: flask.Flask,
) -> None:
    with app.test_request_context("/callback"):
        response = error_handler(
            Error(OAuthError.TEMPORARILY_UNAVAILABLE, retry_after=10)
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "10"
    assert response.json["error"] == "temporarily_unavailable"


@pytest.mark.parametrize(
    ("status", "result", "expected"),
    [
        (
            HTTPStatus.OK,
            {"access_token": "abc", "token_type": "Bearer"},
            {
                "retryable": False,
                "normalized_error": None,
                "invalidate_refresh_token": False,
                "retry_reason": None,
            },
        ),
        (
            HTTPStatus.OK,
            {"error": OAuthError.INVALID_GRANT},
            {
                "retryable": False,
                "normalized_error": OAuthError.INVALID_GRANT,
                "invalidate_refresh_token": False,
                "retry_reason": None,
            },
        ),
        (
            HTTPStatus.BAD_REQUEST,
            {"error": OAuthError.INVALID_GRANT},
            {
                "retryable": False,
                "normalized_error": OAuthError.INVALID_GRANT,
                "invalidate_refresh_token": True,
                "retry_reason": None,
            },
        ),
        (
            HTTPStatus.BAD_REQUEST,
            {"error": OAuthError.INVALID_CLIENT},
            {
                "retryable": False,
                "normalized_error": OAuthError.INVALID_CLIENT,
                "invalidate_refresh_token": False,
                "retry_reason": None,
            },
        ),
        (
            HTTPStatus.BAD_REQUEST,
            {"error": OAuthError.INVALID_REQUEST},
            {
                "retryable": False,
                "normalized_error": OAuthError.INVALID_REQUEST,
                "invalidate_refresh_token": False,
                "retry_reason": None,
            },
        ),
        (
            HTTPStatus.UNAUTHORIZED,
            {"error": OAuthError.INVALID_CLIENT},
            {
                "retryable": False,
                "normalized_error": OAuthError.INVALID_CLIENT,
                "invalidate_refresh_token": False,
                "retry_reason": None,
            },
        ),
        (
            HTTPStatus.TOO_MANY_REQUESTS,
            {"error": OAuthError.TEMPORARILY_UNAVAILABLE},
            {
                "retryable": True,
                "normalized_error": OAuthError.TEMPORARILY_UNAVAILABLE,
                "invalidate_refresh_token": False,
                "retry_reason": RetryReason.RESOURCE_EXHAUSTED,
            },
        ),
        (
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": OAuthError.INVALID_GRANT},
            {
                "retryable": True,
                "normalized_error": OAuthError.TEMPORARILY_UNAVAILABLE,
                "invalidate_refresh_token": False,
                "retry_reason": RetryReason.UNAVAILABLE,
            },
        ),
        (
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": OAuthError.INVALID_CLIENT},
            {
                "retryable": True,
                "normalized_error": OAuthError.TEMPORARILY_UNAVAILABLE,
                "invalidate_refresh_token": False,
                "retry_reason": RetryReason.UNAVAILABLE,
            },
        ),
        (
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": OAuthError.TEMPORARILY_UNAVAILABLE},
            {
                "retryable": True,
                "normalized_error": OAuthError.TEMPORARILY_UNAVAILABLE,
                "invalidate_refresh_token": False,
                "retry_reason": RetryReason.UNAVAILABLE,
            },
        ),
        (
            None,
            OAuthError.SERVER_ERROR.json(),
            {
                "retryable": True,
                "normalized_error": OAuthError.TEMPORARILY_UNAVAILABLE,
                "invalidate_refresh_token": False,
                "retry_reason": RetryReason.UNAVAILABLE,
            },
        ),
        (
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"access_token": "abc", "token_type": "Bearer"},
            {
                "retryable": True,
                "normalized_error": OAuthError.TEMPORARILY_UNAVAILABLE,
                "invalidate_refresh_token": False,
                "retry_reason": RetryReason.UNAVAILABLE,
            },
        ),
        (
            HTTPStatus.OK,
            {
                "access_token": "abc",
                "token_type": "Bearer",
                "refresh_token": "new-refresh",
            },
            {
                "retryable": False,
                "normalized_error": None,
                "invalidate_refresh_token": False,
                "retry_reason": None,
            },
        ),
    ],
)
def test_token_endpoint_outcome(
    app_context: flask.ctx.AppContext,
    status: HTTPStatus | None,
    result: oauth_outcome.OAuthResponse,
    expected: dict[str, object],
) -> None:
    actual = oauth_outcome.token_endpoint_outcome(
        status,
        result,
        retry_status_codes=current_settings.fetch.retry_status_codes,
        error_types=current_settings.fetch.error_types,
    )

    assert actual.retryable == expected["retryable"]
    assert actual.normalized_error == expected["normalized_error"]
    assert actual.invalidate_refresh_token == expected["invalidate_refresh_token"]
    assert actual.retry_reason == expected["retry_reason"]
