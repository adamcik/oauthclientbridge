import urllib.parse
from dataclasses import dataclass
from typing import Callable, Protocol, cast

import pytest
import requests
from flask.testing import FlaskClient
from requests_mock import Mocker

from oauthclientbridge import crypto, db
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import Settings

from .conftest import PostClient, ResponseTuple, TokenTuple


class RequestWithBody(Protocol):
    body: str | bytes | None


@dataclass(frozen=True)
class TokenInputValidationCase:
    name: str
    data: dict[str, str | None]
    expected_error: str
    expected_status: int


@pytest.mark.parametrize(
    "case",
    [
        TokenInputValidationCase(
            name="missing credentials",
            data={},
            expected_error=OAuthError.INVALID_CLIENT,
            expected_status=401,
        ),
        TokenInputValidationCase(
            name="missing grant type",
            data={"grant_type": None},
            expected_error=OAuthError.UNSUPPORTED_GRANT_TYPE,
            expected_status=400,
        ),
        TokenInputValidationCase(
            name="empty grant type",
            data={"grant_type": ""},
            expected_error=OAuthError.UNSUPPORTED_GRANT_TYPE,
            expected_status=400,
        ),
        TokenInputValidationCase(
            name="wrong grant type",
            data={"grant_type": "authorization_code"},
            expected_error=OAuthError.UNSUPPORTED_GRANT_TYPE,
            expected_status=400,
        ),
        TokenInputValidationCase(
            name="missing client id",
            data={"client_id": None},
            expected_error=OAuthError.INVALID_CLIENT,
            expected_status=401,
        ),
        TokenInputValidationCase(
            name="empty client id",
            data={"client_id": ""},
            expected_error=OAuthError.INVALID_CLIENT,
            expected_status=401,
        ),
        TokenInputValidationCase(
            name="empty client id duplicate",
            data={"client_id": ""},
            expected_error=OAuthError.INVALID_CLIENT,
            expected_status=401,
        ),
        TokenInputValidationCase(
            name="missing client secret",
            data={"client_secret": None},
            expected_error=OAuthError.INVALID_CLIENT,
            expected_status=401,
        ),
        TokenInputValidationCase(
            name="empty client secret",
            data={"client_secret": ""},
            expected_error=OAuthError.INVALID_CLIENT,
            expected_status=401,
        ),
        TokenInputValidationCase(
            name="wrong client secret",
            data={"client_secret": "does-not-exist"},
            expected_error=OAuthError.INVALID_CLIENT,
            expected_status=401,
        ),
        TokenInputValidationCase(
            name="scope not supported",
            data={"scope": "foo"},
            expected_error=OAuthError.INVALID_SCOPE,
            expected_status=400,
        ),
        TokenInputValidationCase(
            name="empty scope not supported",
            data={"scope": ""},
            expected_error=OAuthError.INVALID_SCOPE,
            expected_status=400,
        ),
    ],
    ids=lambda case: case.name,
)
def test_token_input_validation(
    post: PostClient,
    case: TokenInputValidationCase,
):
    initial = {
        "client_id": "does-not-exist",
        "client_secret": "wrong-secret",
        "grant_type": "client_credentials",
    }

    for key, value in case.data.items():
        if value is None:
            del initial[key]
        else:
            initial[key] = value

    resp: ResponseTuple = post("/token", initial)

    assert resp.status == case.expected_status
    assert resp.data["error"] == case.expected_error
    assert resp.data["error_description"]


def test_token_invalid_credentials(
    post: PostClient,
    access_token: TokenTuple,
    settings: Settings,
):
    data = {
        "client_id": access_token.client_id,
        "client_secret": "invalid",
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == 401
    assert resp.data["error"] == OAuthError.INVALID_CLIENT
    assert resp.data["error_description"]

    assert "WWW-Authenticate" in resp.headers
    assert resp.headers["WWW-Authenticate"] == 'Basic realm="oauthclientbridge"'


def test_token_multiple_auth_fails(post: PostClient, access_token: TokenTuple):
    auth = (access_token.client_id, access_token.client_secret)

    data = {
        "client_id": access_token.client_id,
        "client_secret": access_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp: ResponseTuple = post("/token", data, auth=auth)

    assert resp.status == 400
    assert resp.data["error"] == OAuthError.INVALID_REQUEST
    assert resp.data["error_description"]


def test_token(post: PostClient, access_token: TokenTuple):
    data = {
        "client_id": access_token.client_id,
        "client_secret": access_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == 200
    assert resp.data == access_token.value


def test_token_normalizes_dashless_uuid_client_id(
    post: PostClient, access_token: TokenTuple
):
    data = {
        "client_id": str(access_token.client_id).replace("-", ""),
        "client_secret": access_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == 200
    assert resp.data == access_token.value


def test_token_normalizes_unpadded_client_secret(
    post: PostClient, access_token: TokenTuple
):
    data = {
        "client_id": access_token.client_id,
        "client_secret": access_token.client_secret.rstrip("="),
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == 200
    assert resp.data == access_token.value


def test_token_rejects_malformed_client_id(post: PostClient, access_token: TokenTuple):
    data = {
        "client_id": "# SPOTIFY_CLIENT_ID",
        "client_secret": access_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == 401
    assert resp.data == {
        "error": OAuthError.INVALID_CLIENT,
        "error_description": "Malformed client_id.",
    }


def test_token_basic_auth(post: PostClient, access_token: TokenTuple):
    auth = (access_token.client_id, access_token.client_secret)
    data = {"grant_type": "client_credentials"}

    resp = post("/token", data, auth=auth)

    assert resp.status == 200
    assert resp.data == access_token.value


@dataclass(frozen=True)
class BadBasicAuthCase:
    name: str
    header: bytes


@pytest.mark.parametrize(
    "case",
    [
        BadBasicAuthCase(name="foo and bar", header=b"Basic Zm9vOmJhcg=="),
        BadBasicAuthCase(name="foo and empty password", header=b"Basic Zm9vOg=="),
        BadBasicAuthCase(name="empty username and bar", header=b"Basic OmJhcg=="),
        BadBasicAuthCase(name="empty username and password", header=b"Basic Og=="),
        BadBasicAuthCase(name="empty credentials", header=b"Basic "),
        BadBasicAuthCase(name="invalid utf8 username 1", header=b"Basic 4zpiYXI="),
        BadBasicAuthCase(name="invalid utf8 username 2", header=b"Basic 6TpiYXI="),
        BadBasicAuthCase(name="invalid base64 padding", header=b"Basic =="),
        BadBasicAuthCase(name="invalid base64 text", header=b"Basic xyz"),
    ],
    ids=lambda case: case.name,
)
def test_token_bad_basic_auth(
    post: PostClient,
    case: BadBasicAuthCase,
    settings: Settings,
):
    headers = {"Authorization": case.header}
    data = {"grant_type": "client_credentials"}

    resp = post("/token", data, headers=headers)

    assert resp.status == 401
    assert resp.data["error"] == OAuthError.INVALID_CLIENT

    assert "WWW-Authenticate" in resp.headers
    assert resp.headers["WWW-Authenticate"] == 'Basic realm="oauthclientbridge"'


def test_token_wrong_method(client: FlaskClient):
    resp = client.get("/token")
    assert resp.status_code == 405


def test_token_revoked(post: PostClient, access_token: TokenTuple):
    data = {
        "client_id": access_token.client_id,
        "client_secret": access_token.client_secret,
        "grant_type": "client_credentials",
    }

    _ = db.update(access_token.client_id, None)  # Revoke directly in the db.

    resp = post("/token", data)

    assert resp.status == 400
    assert resp.data["error"] == OAuthError.INVALID_GRANT
    assert resp.data["error_description"]


def test_token_revoked_returns_workaround_token_for_matching_user_agent(
    post: PostClient,
    access_token: TokenTuple,
    settings: Settings,
):
    settings.revoked_grant_workaround_user_agents = r"^Mopidy-Spotify/4\.1\.1\b"
    settings.revoked_grant_workaround_access_token = (
        "OAUTHCLIENTBRIDGE_REVOKED_GRANT_WORKAROUND"
    )
    settings.revoked_grant_workaround_expires_in = 300

    data = {
        "client_id": access_token.client_id,
        "client_secret": access_token.client_secret,
        "grant_type": "client_credentials",
    }

    _ = db.update(access_token.client_id, None)

    resp = post(
        "/token",
        data,
        headers={"User-Agent": "Mopidy-Spotify/4.1.1 Mopidy/3.4.2 CPython/3.11.2"},
    )

    assert resp.status == 200
    assert resp.data == {
        "access_token": "OAUTHCLIENTBRIDGE_REVOKED_GRANT_WORKAROUND",
        "token_type": "Bearer",
        "expires_in": 300,
    }


def test_token_revoked_workaround_does_not_apply_for_non_matching_user_agent(
    post: PostClient,
    access_token: TokenTuple,
    settings: Settings,
):
    settings.revoked_grant_workaround_user_agents = r"^Mopidy-Spotify/4\.1\.1\b"

    data = {
        "client_id": access_token.client_id,
        "client_secret": access_token.client_secret,
        "grant_type": "client_credentials",
    }

    _ = db.update(access_token.client_id, None)

    resp = post(
        "/token",
        data,
        headers={"User-Agent": "curl/8.8.0"},
    )

    assert resp.status == 400
    assert resp.data["error"] == OAuthError.INVALID_GRANT


def test_token_wrong_secret_and_not_found_identical(
    post: PostClient, access_token: TokenTuple
):
    data1 = {
        "client_id": access_token.client_id,
        "client_secret": "bad-secret",
        "grant_type": "client_credentials",
    }
    data2 = {
        "client_id": db.generate_id(),
        "client_secret": access_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp1 = post("/token", data1)
    resp2 = post("/token", data2)

    assert resp1.data == resp2.data
    assert resp2.status == resp2.status


def test_token_refresh_post_data(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
):
    """Test that expected data gets POSTed to provider."""

    def match(request: requests.Request) -> bool:
        expected: dict[str, list[str]] = {
            "client_id": [settings.oauth.client_id],
            "client_secret": [settings.oauth.client_secret.get_secret_value()],
            "grant_type": [settings.oauth.grant_type],
            "refresh_token": [refresh_token.value["refresh_token"]],
        }
        request_with_body = cast(RequestWithBody, request)
        body = request_with_body.body
        assert isinstance(body, (str, bytes))
        if isinstance(body, str):
            parsed_body = urllib.parse.parse_qs(body)
        else:
            parsed_body = urllib.parse.parse_qs(body)
        assert expected == parsed_body
        return True

    _ = requests_mock.post(
        settings.oauth.token_uri,
        json={"access_token": "abc", "grant_type": "test"},
        additional_matcher=cast(Callable[..., bool], match),
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    _ = post("/token", data)


@dataclass(frozen=True)
class ExtraTokenValuesCase:
    name: str
    response: dict[str, str]
    updated: dict[str, str]


@pytest.mark.parametrize(
    "case",
    [
        ExtraTokenValuesCase(name="no extra values", response={}, updated={}),
        ExtraTokenValuesCase(
            name="scope stays provider only",
            response={"scope": "foo"},
            updated={},
        ),
        ExtraTokenValuesCase(
            name="refresh token replaced",
            response={"refresh_token": "def"},
            updated={"refresh_token": "def"},
        ),
        ExtraTokenValuesCase(
            name="private value ignored",
            response={"private": "123"},
            updated={},
        ),
    ],
    ids=lambda case: case.name,
)
def test_token_with_extra_values(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    case: ExtraTokenValuesCase,
    settings: Settings,
):
    token = {"access_token": "abc", "token_type": "test", "expires_in": 3600}
    token.update(case.response)

    _ = requests_mock.post(
        settings.oauth.token_uri,
        json=token,
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    _ = post("/token", data)

    expected = refresh_token.value.copy()
    expected.update(case.updated)

    # Check that the token we fetched got stored directly in db.
    record = db.lookup(refresh_token.client_id)
    assert record.encrypted_token is not None

    actual = crypto.loads(refresh_token.client_secret, record.encrypted_token)
    assert expected == actual


def test_token_refresh_token_is_not_returned_from_provider(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
):
    _ = requests_mock.post(
        settings.oauth.token_uri,
        json={
            "access_token": "abc",
            "token_type": "test",
            "refresh_token": "def",
        },
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    expected = {"access_token": "abc", "token_type": "test"}

    assert resp.status == 200
    assert resp.data == expected


def test_token_only_returns_values_from_provider(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
):
    token = crypto.dumps(
        refresh_token.client_secret,
        {"refresh_token": "abc", "token_type": "test", "private": "foobar"},
    )
    _ = db.update(refresh_token.client_id, token)

    _ = requests_mock.post(
        settings.oauth.token_uri,
        json={"access_token": "abc", "token_type": "test"},
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    expected = {"access_token": "abc", "token_type": "test"}

    assert resp.status == 200
    assert resp.data == expected


def test_token_cleans_unneeded_data_from_db(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
):
    token = crypto.dumps(
        refresh_token.client_secret,
        {
            "access_token": "abc",
            "token_type": "test",
            "refresh_token": "abc",
            "expires_in": 3600,
        },
    )
    _ = db.update(refresh_token.client_id, token)

    _ = requests_mock.post(
        settings.oauth.token_uri,
        json={"access_token": "abc", "token_type": "test"},
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    _ = post("/token", data)

    expected = {"refresh_token": "abc"}

    # Check that the token we fetched got stored directly in db.
    record = db.lookup(refresh_token.client_id)
    assert record.encrypted_token is not None

    actual = crypto.loads(refresh_token.client_secret, record.encrypted_token)
    assert expected == actual


def test_token_only_returns_scope_from_db(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
):
    token = crypto.dumps(
        refresh_token.client_secret,
        {"refresh_token": "abc", "token_type": "test", "scope": "foobar"},
    )
    _ = db.update(refresh_token.client_id, token)

    _ = requests_mock.post(
        settings.oauth.token_uri,
        json={"access_token": "abc", "token_type": "test"},
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    expected = {"access_token": "abc", "token_type": "test", "scope": "foobar"}

    assert resp.status == 200
    assert resp.data == expected


# TODO: fix expected_error and expected_status
@dataclass(frozen=True)
class TokenProviderErrorCase:
    name: str
    error: str
    expected_error: str
    expected_status: int


@pytest.mark.parametrize(
    "case",
    [
        TokenProviderErrorCase(
            name="oauth invalid request",
            error=OAuthError.INVALID_REQUEST,
            expected_error=OAuthError.INVALID_REQUEST,
            expected_status=400,
        ),
        TokenProviderErrorCase(
            name="oauth invalid client",
            error=OAuthError.INVALID_CLIENT,
            expected_error=OAuthError.INVALID_CLIENT,
            expected_status=401,
        ),
        TokenProviderErrorCase(
            name="oauth invalid grant",
            error=OAuthError.INVALID_GRANT,
            expected_error=OAuthError.INVALID_GRANT,
            expected_status=400,
        ),
        TokenProviderErrorCase(
            name="oauth unauthorized client",
            error=OAuthError.UNAUTHORIZED_CLIENT,
            expected_error=OAuthError.UNAUTHORIZED_CLIENT,
            expected_status=400,
        ),
        TokenProviderErrorCase(
            name="oauth unsupported grant type",
            error=OAuthError.UNSUPPORTED_GRANT_TYPE,
            expected_error=OAuthError.UNSUPPORTED_GRANT_TYPE,
            expected_status=400,
        ),
        TokenProviderErrorCase(
            name="oauth invalid scope",
            error=OAuthError.INVALID_SCOPE,
            expected_error=OAuthError.INVALID_SCOPE,
            expected_status=400,
        ),
        TokenProviderErrorCase(
            name="transient provider error",
            error="errorTransient",
            expected_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_status=503,
        ),
        TokenProviderErrorCase(
            name="unknown provider error",
            error="badError",
            expected_error=OAuthError.SERVER_ERROR,
            expected_status=400,
        ),
    ],
    ids=lambda case: case.name,
)
def test_token_provider_errors(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
    case: TokenProviderErrorCase,
):
    _ = requests_mock.post(
        settings.oauth.token_uri,
        status_code=400,
        json={"error": case.error},
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == case.expected_status
    assert resp.data["error"] == case.expected_error
    assert resp.data["error_description"]


@dataclass(frozen=True)
class InvalidProviderResponseCase:
    name: str
    token: dict[str, str]


@pytest.mark.parametrize(
    "case",
    [
        InvalidProviderResponseCase(name="empty payload", token={}),
        InvalidProviderResponseCase(
            name="missing token type",
            token={"access_token": "abc"},
        ),
        InvalidProviderResponseCase(
            name="missing access token",
            token={"token_type": "test"},
        ),
    ],
    ids=lambda case: case.name,
)
def test_token_provider_invalid_response(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
    case: InvalidProviderResponseCase,
):
    _ = requests_mock.post(
        settings.oauth.token_uri,
        json=case.token,
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == 400
    assert resp.data["error"] == OAuthError.INVALID_REQUEST
    assert resp.data["error_description"]


def test_token_provider_unavailable(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
):
    _ = requests_mock.post(
        settings.oauth.token_uri,
        status_code=503,
        text="Unavailable.",
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == 503
    assert resp.data["error"] == OAuthError.TEMPORARILY_UNAVAILABLE
    assert resp.data["error_description"]


def test_token_invalid_grant_revokes_stored_refresh_token(
    post: PostClient,
    client: FlaskClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
):
    requests_mock.post(
        settings.oauth.token_uri,
        status_code=400,
        json={"error": OAuthError.INVALID_GRANT},
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    first = post("/token", data)
    second = post("/token", data)

    assert first.status == 400
    assert first.data["error"] == OAuthError.INVALID_GRANT
    assert second.status == 400
    assert second.data["error"] == OAuthError.INVALID_GRANT
    record = db.lookup(refresh_token.client_id)
    assert record.encrypted_token is None
    assert len(requests_mock.request_history) == 1

    metrics = client.get("/metrics")
    assert b"oauth_refresh_token_invalidations_total" in metrics.data
    assert b'reason="invalid_grant"' in metrics.data


def test_token_retryable_invalid_grant_does_not_revoke_refresh_token(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
):
    requests_mock.post(
        settings.oauth.token_uri,
        status_code=503,
        json={"error": OAuthError.INVALID_GRANT},
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == 503
    assert resp.data["error"] == OAuthError.TEMPORARILY_UNAVAILABLE
    record = db.lookup(refresh_token.client_id)
    assert record.encrypted_token is not None


def test_token_retryable_invalid_client_returns_temporarily_unavailable(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
):
    requests_mock.post(
        settings.oauth.token_uri,
        status_code=503,
        json={"error": OAuthError.INVALID_CLIENT},
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == 503
    assert resp.data["error"] == OAuthError.TEMPORARILY_UNAVAILABLE
    record = db.lookup(refresh_token.client_id)
    assert record.encrypted_token is not None


def test_token_retryable_refresh_error_returns_retry_after_header(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
):
    requests_mock.post(
        settings.oauth.token_uri,
        status_code=503,
        headers={"Retry-After": "10"},
        json={"error": OAuthError.TEMPORARILY_UNAVAILABLE},
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == 503
    assert resp.data["error"] == OAuthError.TEMPORARILY_UNAVAILABLE
    assert resp.headers["Retry-After"] == "10"


def test_token_terminal_refresh_error_ignores_retry_after_header(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
):
    requests_mock.post(
        settings.oauth.token_uri,
        status_code=400,
        headers={"Retry-After": "10"},
        json={"error": OAuthError.INVALID_GRANT},
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == 400
    assert resp.data["error"] == OAuthError.INVALID_GRANT
    assert "Retry-After" not in resp.headers


# TODO: Test other than basic auth...
# TODO: Test oauth helpers directly?
