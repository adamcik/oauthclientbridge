import urllib.parse
from dataclasses import dataclass

import pytest

from oauthclientbridge import bridge
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
    response = await bridge_harness.bridge.authorize(
        bridge.AuthorizationRequest(query={"state": "caller-state"})
    )

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
    first = await bridge_harness.bridge.authorize(bridge.AuthorizationRequest(query={}))
    second = await bridge_harness.bridge.authorize(
        bridge.AuthorizationRequest(query={})
    )

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

    response = await subject.authorize(
        bridge.AuthorizationRequest(query={"scope": case.scope})
    )

    assert response.status == case.status
    if response.status == 400:
        assert b'"error": "invalid_scope"' in response.body
        assert response.session is None


@pytest.mark.anyio
async def test_authorization_rejects_wrong_redirect_uri(bridge_harness: BridgeHarness):
    response = await bridge_harness.bridge.authorize(
        bridge.AuthorizationRequest(
            query={"redirect_uri": "https://wrong.example.com/callback"}
        )
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

    response = await subject.authorize(bridge.AuthorizationRequest(query={}))

    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(response.headers["Location"]).query
    )
    assert set(query["scope"][0].split()) == {"foo", "bar"}
