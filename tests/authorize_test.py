import urllib.parse

import pytest
from flask.testing import FlaskClient
from requests_mock import Mocker

from oauthclientbridge import crypto, db, errors
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
    redirect_uri = settings.redirect_uri
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
        settings.token_uri,
        json=data,
    )

    resp = get("/callback?code=1234&state=" + state)

    with client.session_transaction() as session:
        assert resp.data["state"] == client_state
        assert "client_state" not in session


@pytest.mark.parametrize(
    "query,expected_error",
    [
        ("", errors.INVALID_REQUEST),
        ("?code", errors.INVALID_STATE),
        ("?state", errors.INVALID_STATE),
        ("?state=&code=", errors.INVALID_STATE),
        ("?code=1234", errors.INVALID_STATE),
        ("?state={state}", errors.INVALID_REQUEST),
        ("?state={state}&code=", errors.INVALID_REQUEST),
        ("?state={state}&error=invalid_request", errors.INVALID_REQUEST),
        ("?state={state}&error=unauthorized_client", errors.UNAUTHORIZED_CLIENT),
        ("?state={state}&error=access_denied", errors.ACCESS_DENIED),
        (
            "?state={state}&error=unsupported_response_type",
            errors.UNSUPPORTED_RESPONSE_TYPE,
        ),
        ("?state={state}&error=invalid_scope", errors.INVALID_SCOPE),
        ("?state={state}&error=server_error", errors.SERVER_ERROR),
        (
            "?state={state}&error=temporarily_unavailable",
            errors.TEMPORARILY_UNAVAILABLE,
        ),
        ("?state={state}&error=badErrorCode", errors.SERVER_ERROR),
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
        ({}, errors.INVALID_RESPONSE, 400),
        ({"token_type": "foobar"}, errors.INVALID_RESPONSE, 400),
        ({"access_token": "foobar"}, errors.INVALID_RESPONSE, 400),
        ({"access_token": "", "token_type": ""}, errors.INVALID_RESPONSE, 400),
        ({"access_token": "foobar", "token_type": ""}, errors.INVALID_RESPONSE, 400),
        ({"access_token": "", "token_type": "foobar"}, errors.INVALID_RESPONSE, 400),
        ({"error": errors.INVALID_REQUEST}, errors.INVALID_REQUEST, 400),
        ({"error": errors.INVALID_CLIENT}, errors.INVALID_CLIENT, 401),
        ({"error": errors.INVALID_GRANT}, errors.INVALID_GRANT, 400),
        ({"error": errors.UNAUTHORIZED_CLIENT}, errors.UNAUTHORIZED_CLIENT, 400),
        ({"error": errors.UNSUPPORTED_GRANT_TYPE}, errors.UNSUPPORTED_GRANT_TYPE, 400),
        ({"error": errors.INVALID_SCOPE}, errors.INVALID_SCOPE, 400),
        ({"error": errors.SERVER_ERROR}, errors.SERVER_ERROR, 400),
        (
            {"error": errors.TEMPORARILY_UNAVAILABLE},
            errors.TEMPORARILY_UNAVAILABLE,
            400,
        ),
        ({"error": "errorTransient"}, errors.TEMPORARILY_UNAVAILABLE, 400),
        ({"error": "badErrorCode"}, errors.SERVER_ERROR, 400),
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
        settings.token_uri,
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
        settings.token_uri,
        text="Not a JSON value",
    )

    resp = get("/callback?code=1234&state=" + state)
    assert resp.status == 400
    assert resp.data["error"] == errors.SERVER_ERROR


def test_callback_authorization_code_stores_token(
    get: GetClient,
    state: str,
    requests_mock: Mocker,
    settings: Settings,
):
    data = {"token_type": "Bearer", "access_token": "1234567890"}
    _ = requests_mock.post(
        settings.token_uri,
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
        settings.token_uri,
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
        settings.token_uri,
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
