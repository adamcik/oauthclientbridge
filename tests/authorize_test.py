import json
import unittest.mock
import urllib.parse
from dataclasses import dataclass

import flask
import pytest
from flask.testing import FlaskClient
from requests_mock import Mocker

from oauthclientbridge import crypto, db
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import Settings
from oauthclientbridge.views import (
    _requested_scope_is_allowed,  # pyright: ignore[reportPrivateUsage] # Direct helper test.
    _set_callback_security_headers,  # pyright: ignore[reportPrivateUsage] # Direct helper test.
)
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


def test_callback_response_has_security_headers(client: FlaskClient):
    response = client.get("/callback")

    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert (
        response.headers["Permissions-Policy"]
        == "geolocation=(), microphone=(), camera=()"
    )
    assert response.headers["Content-Security-Policy"] == (
        "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


def test_callback_csp_can_be_disabled(client: FlaskClient, settings: Settings):
    settings.callback_content_security_policy = None

    response = client.get("/callback")

    assert "Content-Security-Policy" not in response.headers


def test_callback_security_header_helper(app: flask.Flask):
    with app.app_context():
        response = _set_callback_security_headers(
            flask.Response(), "default-src 'none'"
        )

    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert (
        response.headers["Permissions-Policy"]
        == "geolocation=(), microphone=(), camera=()"
    )
    assert response.headers["Content-Security-Policy"] == "default-src 'none'"


@dataclass(frozen=True)
class ScopeCase:
    name: str
    requested_scope: str
    allowed_scopes: set[str] | None
    expected_allowed: bool
    expected_status: int


@pytest.mark.parametrize(
    "case",
    [
        ScopeCase(
            name="exact",
            requested_scope="foo bar",
            allowed_scopes={"foo", "bar"},
            expected_allowed=True,
            expected_status=302,
        ),
        ScopeCase(
            name="subset",
            requested_scope="foo",
            allowed_scopes={"foo", "bar"},
            expected_allowed=True,
            expected_status=302,
        ),
        ScopeCase(
            name="empty",
            requested_scope="",
            allowed_scopes={"foo", "bar"},
            expected_allowed=True,
            expected_status=302,
        ),
        ScopeCase(
            name="duplicate",
            requested_scope="foo foo",
            allowed_scopes={"foo", "bar"},
            expected_allowed=True,
            expected_status=302,
        ),
        ScopeCase(
            name="disallowed",
            requested_scope="foo baz",
            allowed_scopes={"foo", "bar"},
            expected_allowed=False,
            expected_status=400,
        ),
        ScopeCase(
            name="allowlist disabled",
            requested_scope="foo baz",
            allowed_scopes=None,
            expected_allowed=True,
            expected_status=302,
        ),
    ],
    ids=lambda case: case.name,
)
def test_requested_scope_allowlist(case: ScopeCase):
    assert (
        _requested_scope_is_allowed(case.requested_scope, case.allowed_scopes)
        is case.expected_allowed
    )


def test_authorize_uses_configured_scopes_when_scope_is_omitted(
    client: FlaskClient, settings: Settings
):
    settings.oauth = settings.oauth.model_copy(update={"scopes": {"foo", "bar"}})

    response = client.get("/")

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(response.location).query)
    assert set(query["scope"][0].split()) == {"foo", "bar"}


@pytest.mark.parametrize(
    "case",
    [
        ScopeCase(
            name="exact",
            requested_scope="foo bar",
            allowed_scopes={"foo", "bar"},
            expected_allowed=True,
            expected_status=302,
        ),
        ScopeCase(
            name="subset",
            requested_scope="foo",
            allowed_scopes={"foo", "bar"},
            expected_allowed=True,
            expected_status=302,
        ),
        ScopeCase(
            name="empty",
            requested_scope="",
            allowed_scopes={"foo", "bar"},
            expected_allowed=True,
            expected_status=302,
        ),
        ScopeCase(
            name="duplicate",
            requested_scope="foo foo",
            allowed_scopes={"foo", "bar"},
            expected_allowed=True,
            expected_status=302,
        ),
        ScopeCase(
            name="disallowed",
            requested_scope="foo baz",
            allowed_scopes={"foo", "bar"},
            expected_allowed=False,
            expected_status=400,
        ),
    ],
    ids=lambda case: case.name,
)
def test_authorize_enforces_configured_scope_allowlist(
    client: FlaskClient,
    settings: Settings,
    case: ScopeCase,
):
    settings.oauth = settings.oauth.model_copy(
        update={"scopes": {"foo", "bar"}, "allowed_scopes": case.allowed_scopes}
    )

    response = client.get(
        "/?" + urllib.parse.urlencode({"scope": case.requested_scope})
    )

    assert response.status_code == case.expected_status
    if case.expected_status == 400:
        assert json.loads(response.text)["error"] == "invalid_scope"


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


@dataclass(frozen=True)
class CallbackErrorCase:
    name: str
    query: str
    expected_error: str
    expected_status: int


@pytest.mark.parametrize(
    "case",
    [
        CallbackErrorCase(
            name="missing query",
            query="",
            expected_error=OAuthError.INVALID_REQUEST,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="missing stored state with bare code",
            query="?code",
            expected_error=OAuthError.INVALID_STATE,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="missing stored state with bare state",
            query="?state",
            expected_error=OAuthError.INVALID_STATE,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="empty state and code",
            query="?state=&code=",
            expected_error=OAuthError.INVALID_STATE,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="missing stored state with code",
            query="?code=1234",
            expected_error=OAuthError.INVALID_STATE,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="missing code",
            query="?state={state}",
            expected_error=OAuthError.INVALID_REQUEST,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="empty code",
            query="?state={state}&code=",
            expected_error=OAuthError.INVALID_REQUEST,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="oauth invalid request",
            query="?state={state}&error=invalid_request",
            expected_error=OAuthError.INVALID_REQUEST,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="oauth unauthorized client",
            query="?state={state}&error=unauthorized_client",
            expected_error=OAuthError.UNAUTHORIZED_CLIENT,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="oauth access denied",
            query="?state={state}&error=access_denied",
            expected_error=OAuthError.ACCESS_DENIED,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="oauth unsupported response type",
            query="?state={state}&error=unsupported_response_type",
            expected_error=OAuthError.UNSUPPORTED_RESPONSE_TYPE,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="oauth invalid scope",
            query="?state={state}&error=invalid_scope",
            expected_error=OAuthError.INVALID_SCOPE,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="oauth server error",
            query="?state={state}&error=server_error",
            expected_error=OAuthError.SERVER_ERROR,
            expected_status=400,
        ),
        CallbackErrorCase(
            name="oauth temporarily unavailable",
            query="?state={state}&error=temporarily_unavailable",
            expected_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_status=503,
        ),
        CallbackErrorCase(
            name="unknown oauth error",
            query="?state={state}&error=badErrorCode",
            expected_error=OAuthError.SERVER_ERROR,
            expected_status=400,
        ),
    ],
    ids=lambda case: case.name,
)
def test_callback_error_handling(
    case: CallbackErrorCase,
    client_state: str,
    get: GetClient,
    state: str,
):
    resp = get("/callback" + case.query.format(state=state))

    assert resp.status == case.expected_status
    assert resp.data["error"] == case.expected_error
    assert resp.data["state"] == client_state


def test_callback_preserves_retry_after_for_temporarily_unavailable(
    client_state: str,
    get: GetClient,
    state: str,
):
    with unittest.mock.patch(
        "oauthclientbridge.views.oauth.fetch",
        return_value={"error": "temporarily_unavailable", "retry_after": 10},
    ):
        resp = get("/callback?state={state}&code=abc".format(state=state))

    assert resp.status == 503
    assert resp.data["error"] == OAuthError.TEMPORARILY_UNAVAILABLE
    assert resp.data["state"] == client_state
    assert resp.headers["Retry-After"] == "10"


# TODO: Revisit all of the status codes returned, since this is not an API
# endpoint but a callback we can be well behaved with respect to HTTP.
@dataclass(frozen=True)
class AuthorizationCodeErrorCase:
    name: str
    data: dict[str, str]
    expected_error: str
    expected_status: int


@pytest.mark.parametrize(
    "case",
    [
        AuthorizationCodeErrorCase(
            name="empty payload",
            data={},
            expected_error=OAuthError.INVALID_RESPONSE,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="missing access token",
            data={"token_type": "foobar"},
            expected_error=OAuthError.INVALID_RESPONSE,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="missing token type",
            data={"access_token": "foobar"},
            expected_error=OAuthError.INVALID_RESPONSE,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="empty token values",
            data={"access_token": "", "token_type": ""},
            expected_error=OAuthError.INVALID_RESPONSE,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="empty token type",
            data={"access_token": "foobar", "token_type": ""},
            expected_error=OAuthError.INVALID_RESPONSE,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="empty access token",
            data={"access_token": "", "token_type": "foobar"},
            expected_error=OAuthError.INVALID_RESPONSE,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="oauth invalid request",
            data={"error": OAuthError.INVALID_REQUEST},
            expected_error=OAuthError.INVALID_REQUEST,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="oauth invalid client",
            data={"error": OAuthError.INVALID_CLIENT},
            expected_error=OAuthError.INVALID_CLIENT,
            expected_status=401,
        ),
        AuthorizationCodeErrorCase(
            name="oauth invalid grant",
            data={"error": OAuthError.INVALID_GRANT},
            expected_error=OAuthError.INVALID_GRANT,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="oauth unauthorized client",
            data={"error": OAuthError.UNAUTHORIZED_CLIENT},
            expected_error=OAuthError.UNAUTHORIZED_CLIENT,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="oauth unsupported grant type",
            data={"error": OAuthError.UNSUPPORTED_GRANT_TYPE},
            expected_error=OAuthError.UNSUPPORTED_GRANT_TYPE,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="oauth invalid scope",
            data={"error": OAuthError.INVALID_SCOPE},
            expected_error=OAuthError.INVALID_SCOPE,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="oauth server error",
            data={"error": OAuthError.SERVER_ERROR},
            expected_error=OAuthError.SERVER_ERROR,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="oauth temporarily unavailable",
            data={"error": OAuthError.TEMPORARILY_UNAVAILABLE},
            expected_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_status=503,
        ),
        AuthorizationCodeErrorCase(
            name="transient provider error",
            data={"error": "errorTransient"},
            expected_error=OAuthError.TEMPORARILY_UNAVAILABLE,
            expected_status=503,
        ),
        AuthorizationCodeErrorCase(
            name="unknown provider error",
            data={"error": "badErrorCode"},
            expected_error=OAuthError.SERVER_ERROR,
            expected_status=400,
        ),
    ],
    ids=lambda case: case.name,
)
def test_callback_authorization_code_error_handling(
    case: AuthorizationCodeErrorCase,
    get: GetClient,
    state: str,
    requests_mock: Mocker,
    settings: Settings,
):
    _ = requests_mock.post(
        settings.oauth.token_uri,
        json=case.data,
    )

    resp = get("/callback?code=1234&state=" + state)
    assert resp.status == case.expected_status
    assert resp.data["error"] == case.expected_error


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
    record = db.lookup(resp.data["client_id"])
    assert record.encrypted_token is not None
    assert data == crypto.loads(resp.data["client_secret"], record.encrypted_token)


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
    record = db.lookup(resp.data["client_id"])
    assert record.encrypted_token is not None
    assert expected == crypto.loads(resp.data["client_secret"], record.encrypted_token)


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
    record = db.lookup(resp.data["client_id"])
    assert record.encrypted_token is not None
    assert data == crypto.loads(resp.data["client_secret"], record.encrypted_token)


def test_callback_wrong_method(client: FlaskClient, state: str):
    resp = client.post("/callback?code=1234&state=" + state)
    assert resp.status_code == 405


# TODO: Duplicate client-id handling?
# TODO: Wrong methods?
