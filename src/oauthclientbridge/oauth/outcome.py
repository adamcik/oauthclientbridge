from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Mapping

import structlog

from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import current_settings

from .retry import RetryReason, retry_reason_for_status

# TODO: This should be a stricter type or a pydantic model
OAuthResponse = dict[str, Any]

# https://tools.ietf.org/html/rfc6749#section-4.1.2.1
AUTHORIZATION_ERRORS = {
    OAuthError.INVALID_REQUEST,
    OAuthError.UNAUTHORIZED_CLIENT,
    OAuthError.ACCESS_DENIED,
    OAuthError.UNSUPPORTED_RESPONSE_TYPE,
    OAuthError.INVALID_SCOPE,
    OAuthError.SERVER_ERROR,
    OAuthError.TEMPORARILY_UNAVAILABLE,
}

# https://tools.ietf.org/html/rfc6749#section-5.2
TOKEN_ERRORS = {
    OAuthError.INVALID_REQUEST,
    OAuthError.INVALID_CLIENT,
    OAuthError.INVALID_GRANT,
    OAuthError.UNAUTHORIZED_CLIENT,
    OAuthError.UNSUPPORTED_GRANT_TYPE,
    OAuthError.INVALID_SCOPE,
    # These are not really supported by RFC:
    OAuthError.SERVER_ERROR,
    OAuthError.TEMPORARILY_UNAVAILABLE,
}


@dataclass(frozen=True)
class TokenEndpointOutcome:
    retryable: bool
    normalized_error: OAuthError | None
    invalidate_refresh_token: bool
    retry_reason: RetryReason | None = None


def normalize_error(
    error_code: str,
    allowed_types: set[OAuthError],
    fallback_type: OAuthError,
    error_types: Mapping[str, OAuthError] | None = None,
) -> OAuthError:
    """Translate any "bad" error types to something more usable."""
    resolved_error_types = (
        current_settings.fetch.error_types if error_types is None else error_types
    )

    if error_code in resolved_error_types:
        error = resolved_error_types[error_code]
    elif error_code in OAuthError:
        error = OAuthError(error_code)
    else:
        error = fallback_type

    if error not in allowed_types:
        return fallback_type
    return error


def validate_token(token: OAuthResponse) -> bool:
    return bool(token.get("access_token") and token.get("token_type"))


def token_endpoint_outcome(
    status: HTTPStatus | None,
    result: OAuthResponse,
    retry_status_codes: tuple[HTTPStatus, ...],
    error_types: Mapping[str, OAuthError],
    logger: structlog.BoundLogger | None = None,
    endpoint: str | None = None,
) -> TokenEndpointOutcome:
    if logger is not None:
        issue = _unexpected_token_endpoint_issue(status, result, retry_status_codes)
        if issue is not None:
            logger.warning(
                "Unexpected token endpoint response",
                endpoint=endpoint,
                status=status,
                issue=issue,
                result=result,
            )

    if status is None:
        return TokenEndpointOutcome(
            retryable=True,
            normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            invalidate_refresh_token=False,
            retry_reason=RetryReason.UNAVAILABLE,
        )

    if status in retry_status_codes:
        return TokenEndpointOutcome(
            retryable=True,
            normalized_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            invalidate_refresh_token=False,
            retry_reason=retry_reason_for_status(status),
        )

    if status.is_success and validate_token(result):
        return TokenEndpointOutcome(
            retryable=False,
            normalized_error=None,
            invalidate_refresh_token=False,
        )

    if status.is_success:
        return TokenEndpointOutcome(
            retryable=False,
            normalized_error=normalize_error(
                str(result.get("error", OAuthError.INVALID_REQUEST.value)),
                allowed_types=TOKEN_ERRORS,
                fallback_type=OAuthError.INVALID_REQUEST,
                error_types=error_types,
            ),
            invalidate_refresh_token=False,
        )

    normalized_error = None
    if "error" in result:
        normalized_error = normalize_error(
            str(result["error"]),
            allowed_types=TOKEN_ERRORS,
            fallback_type=OAuthError.SERVER_ERROR,
            error_types=error_types,
        )

    return TokenEndpointOutcome(
        retryable=False,
        normalized_error=normalized_error,
        invalidate_refresh_token=(
            status == HTTPStatus.BAD_REQUEST
            and normalized_error == OAuthError.INVALID_GRANT
        ),
    )


def _unexpected_token_endpoint_issue(
    status: HTTPStatus | None,
    result: OAuthResponse,
    retry_status_codes: tuple[HTTPStatus, ...],
) -> str | None:
    if status is None:
        return "transport_failure"
    if status in retry_status_codes:
        if result.get("error") not in {None, OAuthError.TEMPORARILY_UNAVAILABLE.value}:
            return "retryable_status_with_contradictory_oauth_error"
        if validate_token(result):
            return "retryable_status_with_success_payload"
        return None
    if status.is_success and not validate_token(result):
        return "success_status_with_error_payload"
    return None
