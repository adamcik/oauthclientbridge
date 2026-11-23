from freezegun import freeze_time

from oauthclientbridge import oauth
from oauthclientbridge.oauth import (
    _core as oauth_core,  # pyright: ignore[reportPrivateUsage] # Direct implementation test.
)


def test_scrub_refresh_token_preserves_access_token_only_response() -> None:
    token: oauth.OAuthResponse = {
        "access_token": "access-token",
        "expires_in": 3600,
        "token_type": "Bearer",
    }

    assert oauth.scrub_refresh_token(token) == token


def test_scrub_refresh_token_removes_ephemeral_access_token() -> None:
    token: oauth.OAuthResponse = {
        "access_token": "access-token",
        "expires_in": 3600,
        "refresh_token": "refresh-token",
        "token_type": "Bearer",
    }

    assert oauth.scrub_refresh_token(token) == {"refresh_token": "refresh-token"}


def test_parse_retry_with_seconds() -> None:
    assert oauth_core.parse_retry("10") == 10


@freeze_time("2025-01-01 00:00:00 UTC")
def test_parse_retry_with_http_date() -> None:
    # Now
    assert oauth_core.parse_retry("Wed, 01 Jan 2025 00:00:00 GMT") == 0
    # Future date
    assert oauth_core.parse_retry("Fri, 01 Jan 2025 00:00:10 GMT") == 10
    assert oauth_core.parse_retry("Fri, 01 Jan 2025 00:00:30 CET") == 30
    assert oauth_core.parse_retry("Fri, 01 Jan 2025 00:00:00 PST") == 28800
    # Past date
    assert oauth_core.parse_retry("Fri, 01 Jan 2024 00:00:00 GMT") == 0
    assert oauth_core.parse_retry("Fri, 01 Jan 2024 00:00:00 CET") == 0
    assert oauth_core.parse_retry("Fri, 01 Jan 2024 00:00:00 PST") == 0


def test_parse_retry_with_none() -> None:
    assert oauth_core.parse_retry(None) == 0


def test_parse_retry_with_invalid_string() -> None:
    assert oauth_core.parse_retry("invalid") == 0
    assert oauth_core.parse_retry("numb3r") == 0
    assert oauth_core.parse_retry("0x15") == 0


def test_parse_retry_with_multiple_headers() -> None:
    assert oauth_core.parse_retry("10, 20") == 0
    assert oauth_core.parse_retry("10, Wed, 01 Jan 2025 00:00:10 GMT") == 0


def test_sanitize_for_logging_redacts_fields_outside_allowlist() -> None:
    payload = {
        "access_token": "secret-access-token",
        "refresh_token": "secret-refresh-token",
        "id_token": "secret-id-token",
        "client_secret": "secret-client-secret",
        "error": "invalid_grant",
        "error_description": "provider detail",
        "retry_after": 10,
        "client_id": "debug-client-id",
    }

    assert oauth.sanitize_for_logging(payload) == {
        "access_token": "<REDACTED>",
        "refresh_token": "<REDACTED>",
        "id_token": "<REDACTED>",
        "client_secret": "<REDACTED>",
        "error": "invalid_grant",
        "error_description": "provider detail",
        "retry_after": 10,
        "client_id": "debug-client-id",
    }


def test_sanitize_for_logging_redacts_unknown_fields() -> None:
    payload = {"detail": "provider detail", "retry_after": 10}

    assert oauth.sanitize_for_logging(payload) == {
        "detail": "<REDACTED>",
        "retry_after": 10,
    }
