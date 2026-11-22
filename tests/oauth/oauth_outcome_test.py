from dataclasses import dataclass
from http import HTTPStatus

import pytest

from oauthclientbridge import crypto, db, oauth
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.oauth import (
    _outcome as oauth_outcome,  # pyright: ignore[reportPrivateUsage] # Direct implementation test.
)
from oauthclientbridge.oauth._retry import (
    RetryCondition,  # pyright: ignore[reportPrivateUsage] # Direct implementation test.
)
from oauthclientbridge.settings import Settings
from tests.conftest import BridgeHarness


def test_normalize_error_uses_explicit_provider_mapping() -> None:
    assert (
        oauth.normalize_error(
            "provider_error",
            allowed_types={OAuthError.INVALID_GRANT},
            fallback_type=OAuthError.SERVER_ERROR,
            error_types={"provider_error": OAuthError.INVALID_GRANT},
        )
        == OAuthError.INVALID_GRANT
    )
    assert (
        oauth.normalize_error(
            "provider_error",
            allowed_types={OAuthError.INVALID_GRANT},
            fallback_type=OAuthError.SERVER_ERROR,
            error_types={},
        )
        == OAuthError.SERVER_ERROR
    )


def test_normalize_error_filters_mapped_errors_against_allowed_types() -> None:
    assert (
        oauth.normalize_error(
            "provider_error",
            allowed_types={OAuthError.INVALID_GRANT},
            fallback_type=OAuthError.SERVER_ERROR,
            error_types={"provider_error": OAuthError.INVALID_CLIENT},
        )
        == OAuthError.SERVER_ERROR
    )


@pytest.mark.anyio
async def test_token_keeps_invalid_grant_as_400_with_retry_after(
    settings: Settings,
    bridge_harness: BridgeHarness,
) -> None:
    client_id = db.generate_id()
    client_secret = crypto.generate_key()
    db.insert(
        client_id,
        crypto.dumps(client_secret, {"refresh_token": "refresh-token"}),
        settings.database,
    )
    bridge_harness.oauth.results.append({"error": "invalid_grant", "retry_after": 10})
    response = (
        await bridge_harness.bridge.token(
            form={
                "grant_type": "client_credentials",
                "client_id": str(client_id),
                "client_secret": client_secret,
            },
            authorization=None,
            user_agent="",
        )
    ).response

    assert response.status == 400
    assert "Retry-After" not in response.headers
    assert isinstance(response.body, dict)
    assert response.body["error"] == "invalid_grant"


@dataclass(frozen=True)
class TokenEndpointOutcomeCase:
    id: str
    status: HTTPStatus | None
    result: oauth_outcome.OAuthResponse
    expected_retryable: bool
    expected_normalized_error: OAuthError | None
    expected_invalidate_refresh_token: bool
    expected_retry_condition: RetryCondition | None


@pytest.mark.parametrize(
    "case",
    [
        TokenEndpointOutcomeCase(
            id="success token",
            status=HTTPStatus.OK,
            result={"access_token": "abc", "token_type": "Bearer"},
            expected_retryable=False,
            expected_normalized_error=None,
            expected_invalidate_refresh_token=False,
            expected_retry_condition=None,
        ),
        TokenEndpointOutcomeCase(
            id="success invalid grant payload",
            status=HTTPStatus.OK,
            result={"error": OAuthError.INVALID_GRANT},
            expected_retryable=False,
            expected_normalized_error=OAuthError.INVALID_GRANT,
            expected_invalidate_refresh_token=False,
            expected_retry_condition=None,
        ),
        TokenEndpointOutcomeCase(
            id="bad request invalid grant",
            status=HTTPStatus.BAD_REQUEST,
            result={"error": OAuthError.INVALID_GRANT},
            expected_retryable=False,
            expected_normalized_error=OAuthError.INVALID_GRANT,
            expected_invalidate_refresh_token=True,
            expected_retry_condition=None,
        ),
        TokenEndpointOutcomeCase(
            id="bad request invalid client",
            status=HTTPStatus.BAD_REQUEST,
            result={"error": OAuthError.INVALID_CLIENT},
            expected_retryable=False,
            expected_normalized_error=OAuthError.INVALID_CLIENT,
            expected_invalidate_refresh_token=False,
            expected_retry_condition=None,
        ),
        TokenEndpointOutcomeCase(
            id="bad request invalid request",
            status=HTTPStatus.BAD_REQUEST,
            result={"error": OAuthError.INVALID_REQUEST},
            expected_retryable=False,
            expected_normalized_error=OAuthError.INVALID_REQUEST,
            expected_invalidate_refresh_token=False,
            expected_retry_condition=None,
        ),
        TokenEndpointOutcomeCase(
            id="unauthorized invalid client",
            status=HTTPStatus.UNAUTHORIZED,
            result={"error": OAuthError.INVALID_CLIENT},
            expected_retryable=False,
            expected_normalized_error=OAuthError.INVALID_CLIENT,
            expected_invalidate_refresh_token=False,
            expected_retry_condition=None,
        ),
        TokenEndpointOutcomeCase(
            id="too many requests retryable",
            status=HTTPStatus.TOO_MANY_REQUESTS,
            result={"error": OAuthError.TEMPORARILY_UNAVAILABLE},
            expected_retryable=True,
            expected_normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_invalidate_refresh_token=False,
            expected_retry_condition=RetryCondition.RESOURCE_EXHAUSTED,
        ),
        TokenEndpointOutcomeCase(
            id="service unavailable invalid grant",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            result={"error": OAuthError.INVALID_GRANT},
            expected_retryable=True,
            expected_normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_invalidate_refresh_token=False,
            expected_retry_condition=RetryCondition.UNAVAILABLE,
        ),
        TokenEndpointOutcomeCase(
            id="service unavailable invalid client",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            result={"error": OAuthError.INVALID_CLIENT},
            expected_retryable=True,
            expected_normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_invalidate_refresh_token=False,
            expected_retry_condition=RetryCondition.UNAVAILABLE,
        ),
        TokenEndpointOutcomeCase(
            id="service unavailable oauth unavailable",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            result={"error": OAuthError.TEMPORARILY_UNAVAILABLE},
            expected_retryable=True,
            expected_normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_invalidate_refresh_token=False,
            expected_retry_condition=RetryCondition.UNAVAILABLE,
        ),
        TokenEndpointOutcomeCase(
            id="transport failure",
            status=None,
            result=OAuthError.SERVER_ERROR.json(),
            expected_retryable=True,
            expected_normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_invalidate_refresh_token=False,
            expected_retry_condition=RetryCondition.UNAVAILABLE,
        ),
        TokenEndpointOutcomeCase(
            id="service unavailable success payload",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            result={"access_token": "abc", "token_type": "Bearer"},
            expected_retryable=True,
            expected_normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_invalidate_refresh_token=False,
            expected_retry_condition=RetryCondition.UNAVAILABLE,
        ),
        TokenEndpointOutcomeCase(
            id="success token with refresh token",
            status=HTTPStatus.OK,
            result={
                "access_token": "abc",
                "token_type": "Bearer",
                "refresh_token": "new-refresh",
            },
            expected_retryable=False,
            expected_normalized_error=None,
            expected_invalidate_refresh_token=False,
            expected_retry_condition=None,
        ),
    ],
    ids=lambda case: case.id,
)
def test_token_endpoint_outcome(
    settings: Settings,
    case: TokenEndpointOutcomeCase,
) -> None:
    actual = oauth.token_endpoint_outcome(
        case.status,
        case.result,
        retry_status_codes=settings.fetch.retry_status_codes,
        error_types=settings.fetch.error_types,
    )

    assert actual.retryable == case.expected_retryable
    assert actual.normalized_error == case.expected_normalized_error
    assert actual.invalidate_refresh_token == case.expected_invalidate_refresh_token
    assert actual.retry_condition == case.expected_retry_condition
