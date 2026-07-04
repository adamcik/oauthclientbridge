from dataclasses import dataclass
from http import HTTPStatus

import flask.ctx
import pytest

from oauthclientbridge import oauth
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.oauth import outcome as oauth_outcome
from oauthclientbridge.oauth.retry import RetryReason
from oauthclientbridge.settings import current_settings


def test_error_handler_returns_503_for_temporarily_unavailable_retry_after(
    app: flask.Flask,
) -> None:
    with app.test_request_context("/callback"):
        response = oauth.error_handler(
            oauth.Error(OAuthError.TEMPORARILY_UNAVAILABLE, retry_after=10)
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "10"
    assert response.json["error"] == "temporarily_unavailable"


def test_error_handler_keeps_invalid_grant_as_400_with_retry_after(
    app: flask.Flask,
) -> None:
    with app.test_request_context("/token"):
        response = oauth.error_handler(
            oauth.Error(OAuthError.INVALID_GRANT, retry_after=10)
        )

    assert response.status_code == 400
    assert "Retry-After" not in response.headers
    assert response.json["error"] == "invalid_grant"


@dataclass(frozen=True)
class TokenEndpointOutcomeCase:
    name: str
    status: HTTPStatus | None
    result: oauth_outcome.OAuthResponse
    expected_retryable: bool
    expected_normalized_error: OAuthError | None
    expected_invalidate_refresh_token: bool
    expected_retry_reason: RetryReason | None


@pytest.mark.parametrize(
    "case",
    [
        TokenEndpointOutcomeCase(
            name="success token",
            status=HTTPStatus.OK,
            result={"access_token": "abc", "token_type": "Bearer"},
            expected_retryable=False,
            expected_normalized_error=None,
            expected_invalidate_refresh_token=False,
            expected_retry_reason=None,
        ),
        TokenEndpointOutcomeCase(
            name="success invalid grant payload",
            status=HTTPStatus.OK,
            result={"error": OAuthError.INVALID_GRANT},
            expected_retryable=False,
            expected_normalized_error=OAuthError.INVALID_GRANT,
            expected_invalidate_refresh_token=False,
            expected_retry_reason=None,
        ),
        TokenEndpointOutcomeCase(
            name="bad request invalid grant",
            status=HTTPStatus.BAD_REQUEST,
            result={"error": OAuthError.INVALID_GRANT},
            expected_retryable=False,
            expected_normalized_error=OAuthError.INVALID_GRANT,
            expected_invalidate_refresh_token=True,
            expected_retry_reason=None,
        ),
        TokenEndpointOutcomeCase(
            name="bad request invalid client",
            status=HTTPStatus.BAD_REQUEST,
            result={"error": OAuthError.INVALID_CLIENT},
            expected_retryable=False,
            expected_normalized_error=OAuthError.INVALID_CLIENT,
            expected_invalidate_refresh_token=False,
            expected_retry_reason=None,
        ),
        TokenEndpointOutcomeCase(
            name="bad request invalid request",
            status=HTTPStatus.BAD_REQUEST,
            result={"error": OAuthError.INVALID_REQUEST},
            expected_retryable=False,
            expected_normalized_error=OAuthError.INVALID_REQUEST,
            expected_invalidate_refresh_token=False,
            expected_retry_reason=None,
        ),
        TokenEndpointOutcomeCase(
            name="unauthorized invalid client",
            status=HTTPStatus.UNAUTHORIZED,
            result={"error": OAuthError.INVALID_CLIENT},
            expected_retryable=False,
            expected_normalized_error=OAuthError.INVALID_CLIENT,
            expected_invalidate_refresh_token=False,
            expected_retry_reason=None,
        ),
        TokenEndpointOutcomeCase(
            name="too many requests retryable",
            status=HTTPStatus.TOO_MANY_REQUESTS,
            result={"error": OAuthError.TEMPORARILY_UNAVAILABLE},
            expected_retryable=True,
            expected_normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_invalidate_refresh_token=False,
            expected_retry_reason=RetryReason.RESOURCE_EXHAUSTED,
        ),
        TokenEndpointOutcomeCase(
            name="service unavailable invalid grant",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            result={"error": OAuthError.INVALID_GRANT},
            expected_retryable=True,
            expected_normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_invalidate_refresh_token=False,
            expected_retry_reason=RetryReason.UNAVAILABLE,
        ),
        TokenEndpointOutcomeCase(
            name="service unavailable invalid client",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            result={"error": OAuthError.INVALID_CLIENT},
            expected_retryable=True,
            expected_normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_invalidate_refresh_token=False,
            expected_retry_reason=RetryReason.UNAVAILABLE,
        ),
        TokenEndpointOutcomeCase(
            name="service unavailable oauth unavailable",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            result={"error": OAuthError.TEMPORARILY_UNAVAILABLE},
            expected_retryable=True,
            expected_normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_invalidate_refresh_token=False,
            expected_retry_reason=RetryReason.UNAVAILABLE,
        ),
        TokenEndpointOutcomeCase(
            name="transport failure",
            status=None,
            result=OAuthError.SERVER_ERROR.json(),
            expected_retryable=True,
            expected_normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_invalidate_refresh_token=False,
            expected_retry_reason=RetryReason.UNAVAILABLE,
        ),
        TokenEndpointOutcomeCase(
            name="service unavailable success payload",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            result={"access_token": "abc", "token_type": "Bearer"},
            expected_retryable=True,
            expected_normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_invalidate_refresh_token=False,
            expected_retry_reason=RetryReason.UNAVAILABLE,
        ),
        TokenEndpointOutcomeCase(
            name="success token with refresh token",
            status=HTTPStatus.OK,
            result={
                "access_token": "abc",
                "token_type": "Bearer",
                "refresh_token": "new-refresh",
            },
            expected_retryable=False,
            expected_normalized_error=None,
            expected_invalidate_refresh_token=False,
            expected_retry_reason=None,
        ),
    ],
    ids=lambda case: case.name,
)
def test_token_endpoint_outcome(
    app_context: flask.ctx.AppContext,
    case: TokenEndpointOutcomeCase,
) -> None:
    actual = oauth.token_endpoint_outcome(
        case.status,
        case.result,
        retry_status_codes=current_settings.fetch.retry_status_codes,
        error_types=current_settings.fetch.error_types,
    )

    assert actual.retryable == case.expected_retryable
    assert actual.normalized_error == case.expected_normalized_error
    assert actual.invalidate_refresh_token == case.expected_invalidate_refresh_token
    assert actual.retry_reason == case.expected_retry_reason
