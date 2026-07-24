import json
import urllib.parse
from dataclasses import dataclass

import pytest

from oauthclientbridge import bridge, crypto, db
from oauthclientbridge.settings import Settings
from tests.conftest import BridgeHarness


@dataclass(frozen=True)
class AuthorizationCase:
    name: str
    scope: str
    allowed_scopes: set[str] | None
    status: int


@pytest.mark.anyio
async def test_authorization_returns_redirect_and_complete_session(
    settings: Settings, bridge_harness: BridgeHarness
):
    response = await bridge_harness.bridge.authorize(query={"state": "caller-state"})

    location = urllib.parse.urlsplit(response.headers["Location"])
    query = urllib.parse.parse_qs(location.query)

    assert response.status == 302
    assert location.geturl().startswith(settings.oauth.authorization_uri)
    assert query["client_id"] == [settings.oauth.client_id]
    assert query["redirect_uri"] == [settings.oauth.redirect_uri]
    assert response.session == {
        "client_state": "caller-state",
        "state": query["state"][0],
    }
    assert query["state"][0]


@pytest.mark.anyio
async def test_authorization_generates_a_unique_state(bridge_harness: BridgeHarness):
    first = await bridge_harness.bridge.authorize(query={})
    second = await bridge_harness.bridge.authorize(query={})

    assert first.session is not None
    assert second.session is not None
    assert first.session["state"] != second.session["state"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        AuthorizationCase("exact", "foo bar", {"foo", "bar"}, 302),
        AuthorizationCase("subset", "foo", {"foo", "bar"}, 302),
        AuthorizationCase("empty", "", {"foo", "bar"}, 302),
        AuthorizationCase("duplicate", "foo foo", {"foo", "bar"}, 302),
        AuthorizationCase("disallowed", "foo baz", {"foo", "bar"}, 400),
        AuthorizationCase("allowlist disabled", "foo baz", None, 302),
    ],
    ids=lambda case: case.name,
)
async def test_authorization_validates_scope(
    settings: Settings, case: AuthorizationCase, bridge_harness: BridgeHarness
):
    settings.oauth = settings.oauth.model_copy(
        update={"allowed_scopes": case.allowed_scopes}
    )
    subject = bridge.Bridge(settings, bridge_harness.oauth.fetch)

    response = await subject.authorize(query={"scope": case.scope})

    assert response.status == case.status
    if response.status == 400:
        assert b'"error": "invalid_scope"' in response.body
        assert response.session is None


@pytest.mark.anyio
async def test_authorization_rejects_wrong_redirect_uri(bridge_harness: BridgeHarness):
    response = await bridge_harness.bridge.authorize(
        query={"redirect_uri": "https://wrong.example.com/callback"}
    )

    assert response.status == 400
    assert b'"error": "invalid_request"' in response.body
    assert response.session is None


@pytest.mark.anyio
async def test_authorization_uses_configured_scopes_when_omitted(
    settings: Settings, bridge_harness: BridgeHarness
):
    settings.oauth = settings.oauth.model_copy(update={"scopes": {"foo", "bar"}})
    subject = bridge.Bridge(settings, bridge_harness.oauth.fetch)

    response = await subject.authorize(query={})

    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(response.headers["Location"]).query
    )
    assert set(query["scope"][0].split()) == {"foo", "bar"}


@pytest.mark.anyio
async def test_callback_stores_token_and_consumes_session(
    settings: Settings,
    bridge_harness: BridgeHarness,
):
    bridge_harness.oauth.results.append(
        {"token_type": "Bearer", "access_token": "provider-token"}
    )

    response = await bridge_harness.bridge.callback(
        query={"state": "expected-state", "code": "authorization-code"},
        session={"state": "expected-state", "client_state": "caller-state"},
    )

    assert response.status == 200
    assert response.session == {}
    assert b'"state": "caller-state"' in response.body
    assert response.headers["Cache-Control"] == "no-store"
    assert bridge_harness.oauth.calls == [
        (
            settings.oauth.token_uri,
            "token",
            {
                "client_id": settings.oauth.client_id,
                "client_secret": settings.oauth.client_secret.get_secret_value(),
                "code": "authorization-code",
                "grant_type": "authorization_code",
                "redirect_uri": settings.oauth.redirect_uri,
            },
        )
    ]

    assert isinstance(response.body, bytes)
    rendered = json.loads(response.body)
    client_id = rendered["client_id"]
    client_secret = rendered["client_secret"]
    record = db.lookup(db.validate_client_id(client_id), settings.database)
    assert record.encrypted_token is not None
    assert crypto.loads(client_secret, record.encrypted_token) == {
        "token_type": "Bearer",
        "access_token": "provider-token",
    }


@dataclass(frozen=True)
class CallbackValidationCase:
    name: str
    query: dict[str, str]
    session: bridge.Session
    expected_error: str
    expected_status: int


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        CallbackValidationCase(
            name="missing query",
            query={},
            session={"state": "expected-state"},
            expected_error="invalid_request",
            expected_status=400,
        ),
        CallbackValidationCase(
            name="missing session state",
            query={"state": "expected-state", "code": "authorization-code"},
            session={},
            expected_error="invalid_state",
            expected_status=400,
        ),
        CallbackValidationCase(
            name="missing authorization code",
            query={"state": "expected-state"},
            session={"state": "expected-state"},
            expected_error="invalid_request",
            expected_status=400,
        ),
        CallbackValidationCase(
            name="provider denied authorization",
            query={"state": "expected-state", "error": "access_denied"},
            session={"state": "expected-state"},
            expected_error="access_denied",
            expected_status=400,
        ),
    ],
    ids=lambda case: case.name,
)
async def test_callback_validation_returns_rendered_error_and_consumes_session(
    case: CallbackValidationCase, bridge_harness: BridgeHarness
):
    response = await bridge_harness.bridge.callback(
        query=case.query, session=case.session
    )

    assert response.status == case.expected_status
    assert response.session == {}
    assert isinstance(response.body, bytes)
    assert json.loads(response.body)["error"] == case.expected_error
    assert bridge_harness.oauth.calls == []


@pytest.mark.anyio
async def test_callback_rejects_state_mismatch_and_consumes_session(
    bridge_harness: BridgeHarness,
):
    response = await bridge_harness.bridge.callback(
        query={"state": "wrong-state", "code": "authorization-code"},
        session={"state": "expected-state", "client_state": "caller-state"},
    )

    assert response.status == 400
    assert response.session == {}
    assert b'"error": "invalid_state"' in response.body
    assert bridge_harness.oauth.calls == []


@pytest.mark.anyio
async def test_callback_returns_retryable_upstream_error_and_consumes_session(
    bridge_harness: BridgeHarness,
):
    bridge_harness.oauth.results.append(
        {"error": "temporarily_unavailable", "retry_after": 10}
    )

    response = await bridge_harness.bridge.callback(
        query={"state": "expected-state", "code": "authorization-code"},
        session={"state": "expected-state"},
    )

    assert response.status == 503
    assert response.session == {}
    assert response.headers["Retry-After"] == "10"
    assert isinstance(response.body, bytes)
    assert json.loads(response.body)["error"] == "temporarily_unavailable"
