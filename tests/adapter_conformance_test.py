"""HTTP conformance scenarios shared by the Flask and Starlette adapters."""

import urllib.parse
from http import HTTPStatus

import pytest

from oauthclientbridge import crypto, db
from oauthclientbridge.settings import Settings
from tests.conftest import AdapterClient, BridgeHarness

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("adapter_client", ["flask", "starlette"], indirect=True)
async def test_adapter_translates_authorization_redirect(
    settings: Settings, adapter_client: AdapterClient
) -> None:
    async with adapter_client(settings, None) as client:
        response = await client.get("/", params={"state": "caller-state"})

    location = urllib.parse.urlsplit(response.headers["location"])
    query = urllib.parse.parse_qs(location.query)

    assert response.status_code == HTTPStatus.FOUND
    assert location.geturl().startswith(settings.oauth.authorization_uri)
    assert query["client_id"] == [settings.oauth.client_id]
    assert query["redirect_uri"] == [settings.oauth.redirect_uri]
    assert query["state"][0]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.parametrize("adapter_client", ["flask", "starlette"], indirect=True)
async def test_adapter_consumes_callback_session(
    settings: Settings,
    bridge_harness: BridgeHarness,
    adapter_client: AdapterClient,
) -> None:
    bridge_harness.oauth.results.append(
        {"token_type": "Bearer", "access_token": "provider-token"}
    )

    async with adapter_client(settings, bridge_harness.oauth.fetch) as client:
        authorization = await client.get("/", params={"state": "caller-state"})
        state = urllib.parse.parse_qs(
            urllib.parse.urlsplit(authorization.headers["location"]).query
        )["state"][0]
        callback = await client.get(
            "/callback", params={"code": "authorization-code", "state": state}
        )
        replay = await client.get(
            "/callback", params={"code": "authorization-code", "state": state}
        )

    assert callback.status_code == HTTPStatus.OK
    assert callback.headers["content-type"] == "text/html; charset=UTF-8"
    assert callback.headers["cache-control"] == "no-store"
    assert b'"state": "caller-state"' in callback.content
    assert replay.status_code == HTTPStatus.BAD_REQUEST
    assert b'"error": "invalid_state"' in replay.content


@pytest.mark.parametrize("adapter_client", ["flask", "starlette"], indirect=True)
async def test_adapter_translates_token_form_and_basic_auth(
    settings: Settings,
    bridge_harness: BridgeHarness,
    adapter_client: AdapterClient,
) -> None:
    client_secret = crypto.generate_key()
    client_id = db.generate_id()
    db.insert(
        client_id,
        crypto.dumps(
            client_secret,
            {"token_type": "Bearer", "access_token": "stored-token"},
        ),
        settings.database,
    )

    async with adapter_client(settings, bridge_harness.oauth.fetch) as client:
        response = await client.post(
            "/token",
            data={"grant_type": "client_credentials"},
            auth=(str(client_id), client_secret),
            headers={"User-Agent": "adapter-test"},
        )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"token_type": "Bearer", "access_token": "stored-token"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("adapter_client", ["flask", "starlette"], indirect=True)
async def test_adapter_uses_requests_backed_upstream_fetch(
    settings: Settings,
    adapter_client: AdapterClient,
    requests_mock: object,
) -> None:
    requests_mock.post(  # type: ignore[union-attr] # Pytest fixture protocol is external.
        settings.oauth.token_uri,
        json={"token_type": "Bearer", "access_token": "provider-token"},
    )

    async with adapter_client(settings, None) as client:
        authorization = await client.get("/")
        state = urllib.parse.parse_qs(
            urllib.parse.urlsplit(authorization.headers["location"]).query
        )["state"][0]
        callback = await client.get(
            "/callback", params={"code": "authorization-code", "state": state}
        )

    assert callback.status_code == HTTPStatus.OK
