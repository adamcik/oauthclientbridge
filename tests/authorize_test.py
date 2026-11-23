import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus

import httpx
import pytest

from oauthclientbridge import crypto, db
from oauthclientbridge.bridge import (
    _template,  # pyright: ignore[reportPrivateUsage] # Direct template contract test.
)
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import Settings
from tests.conftest import GetClient
from tests.oauth_server import OAuthServer


def test_authorize_redirects(asgi_client: httpx.Client):
    resp = asgi_client.get("/")
    location = urllib.parse.urlsplit(resp.headers["location"])
    assert resp.status_code == 302
    assert location.netloc == "provider.example.com"
    assert location.path == "/auth"
    assert urllib.parse.parse_qs(location.query)["state"][0]


def test_authorize_wrong_method(asgi_client: httpx.Client):
    resp = asgi_client.post("/")
    assert resp.status_code == 405


def test_authorize_redirect_uri(asgi_client: httpx.Client, settings: Settings):
    redirect_uri = settings.oauth.redirect_uri
    url = "/?{}".format(urllib.parse.urlencode({"redirect_uri": redirect_uri}))
    resp = asgi_client.get(url)
    assert resp.status_code == 302


def test_authorize_wrong_redirect_uri(asgi_client: httpx.Client):
    url = "/?{}".format(urllib.parse.urlencode({"redirect_uri": "wrong-value"}))
    resp = asgi_client.get(url)
    assert resp.status_code == 400


def test_authorize_client_state(asgi_client: httpx.Client):
    authorization = asgi_client.get("/?state=s3cret")
    state = urllib.parse.parse_qs(
        urllib.parse.urlsplit(authorization.headers["location"]).query
    )["state"][0]
    response = asgi_client.get(
        "/callback", params={"state": state, "error": "access_denied"}
    )
    assert response.json()["state"] == "s3cret"


def test_authorize_replaces_previous_bridge_session(asgi_client: httpx.Client):
    first = asgi_client.get("/", params={"state": "previous-client-state"})
    previous_state = urllib.parse.parse_qs(
        urllib.parse.urlsplit(first.headers["location"]).query
    )["state"][0]
    second = asgi_client.get("/")
    state = urllib.parse.parse_qs(
        urllib.parse.urlsplit(second.headers["location"]).query
    )["state"][0]
    assert state != previous_state
    response = asgi_client.get(
        "/callback", params={"state": state, "error": "access_denied"}
    )
    assert response.json().get("state") is None


def test_callback_response_has_security_headers(asgi_client: httpx.Client):
    response = asgi_client.get("/callback")

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


def test_callback_csp_can_be_disabled(asgi_client: httpx.Client, settings: Settings):
    settings.callback_content_security_policy = None

    response = asgi_client.get("/callback")

    assert "Content-Security-Policy" not in response.headers


def test_callback_template_adds_security_headers():
    response = _template.render_template(
        "callback",
        {},
        status=HTTPStatus.OK,
        content_security_policy="default-src 'none'",
    )

    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert (
        response.headers["Permissions-Policy"]
        == "geolocation=(), microphone=(), camera=()"
    )
    assert response.headers["Content-Security-Policy"] == "default-src 'none'"


def test_callback_authorization_client_state(
    asgi_client: httpx.Client,
    asgi_get: GetClient,
    asgi_client_state: str,
    asgi_state: str,
    upstream_server: OAuthServer,
    settings: Settings,
):
    data = {"token_type": "Bearer", "access_token": "1234567890"}
    upstream_server.expect("/token").respond(data)

    resp = asgi_get("/callback?code=1234&state=" + asgi_state)

    assert resp.data["state"] == asgi_client_state
    replay = asgi_client.get("/callback?code=1234&state=" + asgi_state)
    assert replay.status_code == 400
    assert replay.json()["error"] == OAuthError.INVALID_STATE
    assert replay.json().get("state") is None


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
    asgi_client_state: str,
    asgi_get: GetClient,
    asgi_state: str,
):
    resp = asgi_get("/callback" + case.query.format(state=asgi_state))

    assert resp.status == case.expected_status
    assert resp.data["error"] == case.expected_error
    assert resp.data["state"] == asgi_client_state


def test_callback_preserves_retry_after_for_temporarily_unavailable(
    asgi_client_state: str,
    asgi_get: GetClient,
    asgi_state: str,
    upstream_server: OAuthServer,
    settings: Settings,
):
    for _ in range(settings.fetch.total_attempts):
        upstream_server.expect("/token").respond(
            {"error": "temporarily_unavailable"},
            status=503,
            headers={"Retry-After": "10"},
        )
    resp = asgi_get(f"/callback?state={asgi_state}&code=abc")

    assert resp.status == 503
    assert resp.data["error"] == OAuthError.TEMPORARILY_UNAVAILABLE
    assert resp.data["state"] == asgi_client_state
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
            expected_error=OAuthError.INVALID_REQUEST,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="missing access token",
            data={"token_type": "foobar"},
            expected_error=OAuthError.INVALID_REQUEST,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="missing token type",
            data={"access_token": "foobar"},
            expected_error=OAuthError.INVALID_REQUEST,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="empty token values",
            data={"access_token": "", "token_type": ""},
            expected_error=OAuthError.INVALID_REQUEST,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="empty token type",
            data={"access_token": "foobar", "token_type": ""},
            expected_error=OAuthError.INVALID_REQUEST,
            expected_status=400,
        ),
        AuthorizationCodeErrorCase(
            name="empty access token",
            data={"access_token": "", "token_type": "foobar"},
            expected_error=OAuthError.INVALID_REQUEST,
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
            expected_error=OAuthError.INVALID_REQUEST,
            expected_status=400,
        ),
    ],
    ids=lambda case: case.name,
)
def test_callback_authorization_code_error_handling(
    case: AuthorizationCodeErrorCase,
    asgi_get: GetClient,
    asgi_state: str,
    upstream_server: OAuthServer,
    settings: Settings,
):
    upstream_server.expect("/token").respond(case.data)

    resp = asgi_get("/callback?code=1234&state=" + asgi_state)
    assert resp.status == case.expected_status
    assert resp.data["error"] == case.expected_error


# TODO: Test with more status codes from callback...
def test_callback_authorization_code_invalid_response(
    asgi_get: GetClient,
    asgi_state: str,
    upstream_server: OAuthServer,
    settings: Settings,
):
    upstream_server.expect("/token").respond(content=b"Not a JSON value")

    resp = asgi_get("/callback?code=1234&state=" + asgi_state)
    assert resp.status == 400
    assert resp.data["error"] == OAuthError.SERVER_ERROR


def test_callback_authorization_code_stores_token(
    asgi_get: GetClient,
    asgi_state: str,
    upstream_server: OAuthServer,
    settings: Settings,
):
    data = {"token_type": "Bearer", "access_token": "1234567890"}
    upstream_server.expect("/token").respond(data)

    resp = asgi_get("/callback?code=1234&state=" + asgi_state)

    # Peek inside internals to check that our token got stored.
    record = db.lookup(resp.data["client_id"], settings.database)
    assert record.encrypted_token is not None
    assert data == crypto.loads(resp.data["client_secret"], record.encrypted_token)


def test_callback_authorization_code_store_refresh_token(
    asgi_get: GetClient,
    asgi_state: str,
    upstream_server: OAuthServer,
    settings: Settings,
):
    token = {
        "token_type": "test",
        "refresh_token": "abc",
        "scope": "foo",
        "access_token": "123",
        "expires_in": 3600,
    }
    upstream_server.expect("/token").respond(token)

    resp = asgi_get("/callback?code=1234&state=" + asgi_state)

    expected = {"refresh_token": "abc", "scope": "foo"}

    # Peek inside internals to check that our token got stored.
    record = db.lookup(resp.data["client_id"], settings.database)
    assert record.encrypted_token is not None
    assert expected == crypto.loads(resp.data["client_secret"], record.encrypted_token)


def test_callback_authorization_code_store_unknown(
    asgi_get: GetClient,
    asgi_state: str,
    upstream_server: OAuthServer,
    settings: Settings,
):
    data = {"token_type": "Bearer", "access_token": "123", "private": "foobar"}
    upstream_server.expect("/token").respond(data)

    resp = asgi_get("/callback?code=1234&state=" + asgi_state)

    # Peek inside internals to check that our token got stored.
    record = db.lookup(resp.data["client_id"], settings.database)
    assert record.encrypted_token is not None
    assert data == crypto.loads(resp.data["client_secret"], record.encrypted_token)


def test_callback_wrong_method(asgi_client: httpx.Client, asgi_state: str):
    resp = asgi_client.post("/callback?code=1234&state=" + asgi_state)
    assert resp.status_code == 405


# TODO: Duplicate client-id handling?
# TODO: Wrong methods?
