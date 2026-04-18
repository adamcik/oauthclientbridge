import urllib.parse

import pytest
from flask.testing import FlaskClient
from requests_mock import Mocker

from oauthclientbridge import crypto, db
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import Settings
from tests.conftest import GetClient


def test_authorize_redirects(client: FlaskClient):
    resp = client.get("/")
    location = urllib.parse.urlsplit(resp.location)

    with client.session_transaction() as session:
        assert resp.status_code == 302
        assert location.netloc == "provider.example.com"
        assert location.path == "/auth"
        assert "state" in session


def test_authorize_wrong_method(client: FlaskClient):
    resp = client.post("/")
    assert resp.status_code == 405


def test_authorize_redirect_uri(client: FlaskClient, settings: Settings):
    redirect_uri = settings.oauth.redirect_uri
    url = "/?%s" % urllib.parse.urlencode({"redirect_uri": redirect_uri})
    resp = client.get(url)
    assert resp.status_code == 302


def test_authorize_wrong_redirect_uri(client: FlaskClient):
    url = "/?%s" % urllib.parse.urlencode({"redirect_uri": "wrong-value"})
    resp = client.get(url)
    assert resp.status_code == 400


def test_authorize_client_state(client: FlaskClient):
    resp = client.get("/?state=s3cret")

    with client.session_transaction() as session:
        assert resp.status_code == 302
        assert session["client_state"] == "s3cret"


def test_callback_authorization_client_state(
    client: FlaskClient,
    get: GetClient,
    client_state: str,
    state: str,
    requests_mock: Mocker,
    settings: Settings,
):
    data = {"token_type": "Bearer", "access_token": "1234567890"}
    _ = requests_mock.post(
        settings.oauth.token_uri,
        json=data,
    )

    resp = get("/callback?code=1234&state=" + state)

    with client.session_transaction() as session:
        assert resp.data["state"] == client_state
        assert "client_state" not in session


@pytest.mark.parametrize(
    "query,expected_error",
    [
        ("", OAuthError.INVALID_REQUEST),
        ("?code", OAuthError.INVALID_STATE),
        ("?state", OAuthError.INVALID_STATE),
        ("?state=&code=", OAuthError.INVALID_STATE),
        ("?code=1234", OAuthError.INVALID_STATE),
        ("?state={state}", OAuthError.INVALID_REQUEST),
        ("?state={state}&code=", OAuthError.INVALID_REQUEST),
        ("?state={state}&error=invalid_request", OAuthError.INVALID_REQUEST),
        ("?state={state}&error=unauthorized_client", OAuthError.UNAUTHORIZED_CLIENT),
        ("?state={state}&error=access_denied", OAuthError.ACCESS_DENIED),
        (
            "?state={state}&error=unsupported_response_type",
            OAuthError.UNSUPPORTED_RESPONSE_TYPE,
        ),
        ("?state={state}&error=invalid_scope", OAuthError.INVALID_SCOPE),
        ("?state={state}&error=server_error", OAuthError.SERVER_ERROR),
        (
            "?state={state}&error=temporarily_unavailable",
            OAuthError.TEMPORARILY_UNAVAILABLE,
        ),
        ("?state={state}&error=badErrorCode", OAuthError.SERVER_ERROR),
    ],
)
def test_callback_error_handling(
    query: str, expected_error: str, client_state: str, get: GetClient, state: str
):
    resp = get("/callback" + query.format(state=state))

    assert resp.status == 400
    assert resp.data["error"] == expected_error
    assert resp.data["state"] == client_state


# TODO: Revisit all of the status codes returned, since this is not an API
# endpoint but a callback we can be well behaved with respect to HTTP.
@pytest.mark.parametrize(
    "data,expected_error,expected_status",
    [
        ({}, OAuthError.INVALID_RESPONSE, 400),
        ({"token_type": "foobar"}, OAuthError.INVALID_RESPONSE, 400),
        ({"access_token": "foobar"}, OAuthError.INVALID_RESPONSE, 400),
        ({"access_token": "", "token_type": ""}, OAuthError.INVALID_RESPONSE, 400),
        (
            {"access_token": "foobar", "token_type": ""},
            OAuthError.INVALID_RESPONSE,
            400,
        ),
        (
            {"access_token": "", "token_type": "foobar"},
            OAuthError.INVALID_RESPONSE,
            400,
        ),
        ({"error": OAuthError.INVALID_REQUEST}, OAuthError.INVALID_REQUEST, 400),
        ({"error": OAuthError.INVALID_CLIENT}, OAuthError.INVALID_CLIENT, 401),
        ({"error": OAuthError.INVALID_GRANT}, OAuthError.INVALID_GRANT, 400),
        (
            {"error": OAuthError.UNAUTHORIZED_CLIENT},
            OAuthError.UNAUTHORIZED_CLIENT,
            400,
        ),
        (
            {"error": OAuthError.UNSUPPORTED_GRANT_TYPE},
            OAuthError.UNSUPPORTED_GRANT_TYPE,
            400,
        ),
        ({"error": OAuthError.INVALID_SCOPE}, OAuthError.INVALID_SCOPE, 400),
        ({"error": OAuthError.SERVER_ERROR}, OAuthError.SERVER_ERROR, 400),
        (
            {"error": OAuthError.TEMPORARILY_UNAVAILABLE},
            OAuthError.TEMPORARILY_UNAVAILABLE,
            400,
        ),
        ({"error": "errorTransient"}, OAuthError.TEMPORARILY_UNAVAILABLE, 400),
        ({"error": "badErrorCode"}, OAuthError.SERVER_ERROR, 400),
    ],
)
def test_callback_authorization_code_error_handling(
    data: dict[str, str],
    expected_error: str,
    expected_status: int,
    get: GetClient,
    state: str,
    requests_mock: Mocker,
    settings: Settings,
):
    _ = requests_mock.post(
        settings.oauth.token_uri,
        json=data,
    )

    resp = get("/callback?code=1234&state=" + state)
    assert resp.status == expected_status
    assert resp.data["error"] == expected_error


# TODO: Test with more status codes from callback...
def test_callback_authorization_code_invalid_response(
    get: GetClient,
    state: str,
    requests_mock: Mocker,
    settings: Settings,
):
    _ = requests_mock.post(
        settings.oauth.token_uri,
        text="Not a JSON value",
    )

    resp = get("/callback?code=1234&state=" + state)
    assert resp.status == 400
    assert resp.data["error"] == OAuthError.SERVER_ERROR


def test_callback_authorization_code_stores_token(
    get: GetClient,
    state: str,
    requests_mock: Mocker,
    settings: Settings,
):
    data = {"token_type": "Bearer", "access_token": "1234567890"}
    _ = requests_mock.post(
        settings.oauth.token_uri,
        json=data,
    )

    resp = get("/callback?code=1234&state=" + state)

    # Peek inside internals to check that our token got stored.
    encrypted = db.lookup(resp.data["client_id"])
    assert encrypted is not None
    assert data == crypto.loads(resp.data["client_secret"], encrypted)


def test_callback_authorization_code_store_refresh_token(
    get: GetClient,
    state: str,
    requests_mock: Mocker,
    settings: Settings,
):
    token = {
        "token_type": "test",
        "refresh_token": "abc",
        "scope": "foo",
        "access_token": "123",
        "expires_in": 3600,
    }
    _ = requests_mock.post(
        settings.oauth.token_uri,
        json=token,
    )

    resp = get("/callback?code=1234&state=" + state)

    expected = {"refresh_token": "abc", "scope": "foo"}

    # Peek inside internals to check that our token got stored.
    encrypted = db.lookup(resp.data["client_id"])
    assert encrypted is not None
    assert expected == crypto.loads(resp.data["client_secret"], encrypted)


def test_callback_authorization_code_store_unknown(
    get: GetClient,
    state: str,
    requests_mock: Mocker,
    settings: Settings,
):
    data = {"token_type": "Bearer", "access_token": "123", "private": "foobar"}
    _ = requests_mock.post(
        settings.oauth.token_uri,
        json=data,
    )

    resp = get("/callback?code=1234&state=" + state)

    # Peek inside internals to check that our token got stored.
    encrypted = db.lookup(resp.data["client_id"])
    assert encrypted is not None
    assert data == crypto.loads(resp.data["client_secret"], encrypted)


def test_callback_wrong_method(client: FlaskClient, state: str):
    resp = client.post("/callback?code=1234&state=" + state)
    assert resp.status_code == 405


# TODO: Duplicate client-id handling?
# TODO: Wrong methods?
