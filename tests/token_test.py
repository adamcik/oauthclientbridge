import urllib.parse

import pytest
from flask.testing import FlaskClient
from requests_mock import Mocker

from oauthclientbridge import crypto, db, errors
from oauthclientbridge.settings import Settings

from .conftest import PostClient, ResponseTuple, TokenTuple


@pytest.mark.parametrize(
    "data,expected_error,expected_status",
    [
        ({}, errors.INVALID_CLIENT, 401),
        ({"grant_type": None}, errors.UNSUPPORTED_GRANT_TYPE, 400),
        ({"grant_type": ""}, errors.UNSUPPORTED_GRANT_TYPE, 400),
        (
            {"grant_type": "authorization_code"},
            errors.UNSUPPORTED_GRANT_TYPE,
            400,
        ),
        ({"client_id": None}, errors.INVALID_CLIENT, 401),
        ({"client_id": ""}, errors.INVALID_CLIENT, 401),
        ({"client_id": ""}, errors.INVALID_CLIENT, 401),
        ({"client_secret": None}, errors.INVALID_CLIENT, 401),
        ({"client_secret": ""}, errors.INVALID_CLIENT, 401),
        ({"client_secret": "does-not-exist"}, errors.INVALID_CLIENT, 401),
        ({"scope": "foo"}, errors.INVALID_SCOPE, 400),
        ({"scope": ""}, errors.INVALID_SCOPE, 400),
    ],
)
def test_token_input_validation(
    post: PostClient,
    data: dict[str, str | None],
    expected_error: str,
    expected_status: int,
):
    initial = {
        "client_id": "does-not-exist",
        "client_secret": "wrong-secret",
        "grant_type": "client_credentials",
    }

    for key, value in data.items():
        if value is None:
            del initial[key]
        else:
            initial[key] = value

    resp: ResponseTuple = post("/token", initial)

    assert resp.status == expected_status
    assert resp.data["error"] == expected_error
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
    assert resp.data["error"] == errors.INVALID_CLIENT
    assert resp.data["error_description"]

    assert "WWW-Authenticate" in resp.headers
    assert (
        resp.headers["WWW-Authenticate"]
        == f'Basic realm="{settings.bridge.auth_realm}"'
    )


def test_token_multiple_auth_fails(post: PostClient, access_token: TokenTuple):
    auth = (access_token.client_id, access_token.client_secret)

    data = {
        "client_id": access_token.client_id,
        "client_secret": access_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp: ResponseTuple = post("/token", data, auth=auth)

    assert resp.status == 400
    assert resp.data["error"] == errors.INVALID_REQUEST
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


def test_token_basic_auth(post: PostClient, access_token: TokenTuple):
    auth = (access_token.client_id, access_token.client_secret)
    data = {"grant_type": "client_credentials"}

    resp = post("/token", data, auth=auth)

    assert resp.status == 200
    assert resp.data == access_token.value


@pytest.mark.parametrize(
    "base64_basic_auth",
    [
        b"Basic Zm9vOmJhcg==",  # 'foo:bar'
        b"Basic Zm9vOg==",  # 'foo:'
        b"Basic OmJhcg==",  # ':bar'
        b"Basic Og==",  # ':'
        b"Basic ",  # ''
        b"Basic 4zpiYXI=",  # '\xe3o:bar'
        b"Basic 6TpiYXI=",  # \xE9:bar'
        b"Basic ==",  # invalid
        b"Basic xyz",  # invalid
    ],
)
def test_token_bad_basic_auth(
    post: PostClient,
    base64_basic_auth: str,
    settings: Settings,
):
    headers = {"Authorization": base64_basic_auth}
    data = {"grant_type": "client_credentials"}

    resp = post("/token", data, headers=headers)

    assert resp.status == 401
    assert resp.data["error"] == errors.INVALID_CLIENT

    assert "WWW-Authenticate" in resp.headers
    assert (
        resp.headers["WWW-Authenticate"]
        == f'Basic realm="{settings.bridge.auth_realm}"'
    )


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
    assert resp.data["error"] == errors.INVALID_GRANT
    assert resp.data["error_description"]


def test_token_wrong_secret_and_not_found_identical(
    post: PostClient, access_token: TokenTuple
):
    data1 = {
        "client_id": access_token.client_id,
        "client_secret": "bad-secret",
        "grant_type": "client_credentials",
    }
    data2 = {
        "client_id": "bad-client",
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

    def match(request):
        expected: dict[str, list[str]] = {
            "client_id": [settings.oauth.client_id],
            "client_secret": [settings.oauth.client_secret.get_secret_value()],
            "grant_type": [settings.oauth.grant_type],
            "refresh_token": [refresh_token.value["refresh_token"]],
        }
        assert expected == urllib.parse.parse_qs(request.body)
        return True

    _ = requests_mock.post(
        settings.oauth.token_uri,
        json={"access_token": "abc", "grant_type": "test"},
        additional_matcher=match,
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    _ = post("/token", data)


@pytest.mark.parametrize(
    "response,updated",
    [
        ({}, {}),
        ({"scope": "foo"}, {}),
        ({"refresh_token": "def"}, {"refresh_token": "def"}),
        ({"private": "123"}, {}),
    ],
)
def test_token_with_extra_values(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    response: dict[str, str],
    updated: dict[str, str],
    settings: Settings,
):
    token = {"access_token": "abc", "token_type": "test", "expires_in": 3600}
    token.update(response)

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
    expected.update(updated)

    # Check that the token we fetched got stored directly in db.
    encrypted = db.lookup(refresh_token.client_id)
    assert encrypted is not None

    actual = crypto.loads(refresh_token.client_secret, encrypted)
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
    encrypted = db.lookup(refresh_token.client_id)
    assert encrypted is not None

    actual = crypto.loads(refresh_token.client_secret, encrypted)
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
@pytest.mark.parametrize(
    "error,expected_error,expected_status",
    [
        (errors.INVALID_REQUEST, errors.INVALID_REQUEST, 400),
        (errors.INVALID_CLIENT, errors.INVALID_CLIENT, 401),
        (errors.INVALID_GRANT, errors.INVALID_GRANT, 400),
        (errors.UNAUTHORIZED_CLIENT, errors.UNAUTHORIZED_CLIENT, 400),
        (errors.UNSUPPORTED_GRANT_TYPE, errors.UNSUPPORTED_GRANT_TYPE, 400),
        (errors.INVALID_SCOPE, errors.INVALID_SCOPE, 400),
        ("errorTransient", errors.TEMPORARILY_UNAVAILABLE, 400),
        ("badError", errors.SERVER_ERROR, 400),
    ],
)
def test_token_provider_errors(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
    error: str,
    expected_error: str,
    expected_status: int,
):
    _ = requests_mock.post(
        settings.oauth.token_uri,
        status_code=400,
        json={"error": error},
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == expected_status
    assert resp.data["error"] == expected_error
    assert resp.data["error_description"]


@pytest.mark.parametrize(
    "token",
    [
        {},
        {"access_token": "abc"},
        {"token_type": "test"},
    ],
)
def test_token_provider_invalid_response(
    post: PostClient,
    refresh_token: TokenTuple,
    requests_mock: Mocker,
    settings: Settings,
    token: dict[str, str],
):
    _ = requests_mock.post(
        settings.oauth.token_uri,
        json=token,
    )

    data = {
        "client_id": refresh_token.client_id,
        "client_secret": refresh_token.client_secret,
        "grant_type": "client_credentials",
    }

    resp = post("/token", data)

    assert resp.status == 400
    assert resp.data["error"] == errors.INVALID_REQUEST
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

    assert resp.status == 400  # TODO: Make this a 503?
    assert resp.data["error"] == errors.TEMPORARILY_UNAVAILABLE
    assert resp.data["error_description"]


# TODO: Test other than basic auth...
# TODO: Test oauth helpers directly?
