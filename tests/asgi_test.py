import urllib.parse
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request

from oauthclientbridge import bridge, crypto, db, types
from oauthclientbridge.asgi import create_app
from oauthclientbridge.routes import (
    _response,  # pyright: ignore[reportPrivateUsage] # Adapter session-translation test.
)
from oauthclientbridge.settings import Settings
from tests.conftest import BridgeHarness

type AsgiClient = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


@dataclass
class RecordingFallbackObserver:
    failures: list[tuple[types.Endpoint, BaseException]] = field(default_factory=list)

    def observe(self, endpoint: types.Endpoint, exception: BaseException) -> None:
        self.failures.append((endpoint, exception))


@pytest.fixture
def asgi_client() -> AsgiClient:
    @asynccontextmanager
    async def _asgi_client(
        app: Starlette, *, raise_app_exceptions: bool = True
    ) -> AsyncIterator[httpx.AsyncClient]:
        transport = httpx.ASGITransport(
            app=app, raise_app_exceptions=raise_app_exceptions
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://bridge.example.com",
            follow_redirects=False,
        ) as client:
            yield client

    return _asgi_client


def test_starlette_adapter_replaces_only_bridge_session_values() -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "session": {
                "unrelated": "value",
                "oauthclientbridge": {"state": "stale-state"},
            },
        }
    )

    _ = _response(
        request,
        bridge.BridgeResponse(
            status=HTTPStatus.OK,
            session={"state": "next-state"},
        ),
    )

    assert request.session == {
        "unrelated": "value",
        "oauthclientbridge": {"state": "next-state"},
    }

    _ = _response(request, bridge.BridgeResponse(status=HTTPStatus.OK, session={}))

    assert request.session == {"unrelated": "value"}


@pytest.mark.anyio
async def test_starlette_adapter_translates_authorization_request(
    settings: Settings, asgi_client: AsgiClient
) -> None:
    app = create_app(settings)

    async with asgi_client(app) as client:
        response = await client.get("/", params={"state": "caller-state"})

    location = urllib.parse.urlsplit(response.headers["location"])
    query = urllib.parse.parse_qs(location.query)

    assert response.status_code == 302
    assert location.geturl().startswith(settings.oauth.authorization_uri)
    assert query["client_id"] == [settings.oauth.client_id]
    assert query["redirect_uri"] == [settings.oauth.redirect_uri]
    assert query["state"][0]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.anyio
async def test_starlette_adapter_consumes_callback_session(
    settings: Settings, bridge_harness: BridgeHarness, asgi_client: AsgiClient
) -> None:
    bridge_harness.oauth.results.append(
        {"token_type": "Bearer", "access_token": "provider-token"}
    )

    app = create_app(settings, fetch=bridge_harness.oauth.fetch)

    async with asgi_client(app) as client:
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

    assert callback.status_code == 200
    assert callback.headers["content-type"] == "text/html; charset=UTF-8"
    assert callback.headers["cache-control"] == "no-store"
    assert b'"state": "caller-state"' in callback.content
    assert replay.status_code == 400
    assert b'"error": "invalid_state"' in replay.content


@pytest.mark.anyio
async def test_starlette_adapter_translates_token_form_and_basic_auth(
    settings: Settings, bridge_harness: BridgeHarness, asgi_client: AsgiClient
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
    app = create_app(settings, fetch=bridge_harness.oauth.fetch)

    async with asgi_client(app) as client:
        response = await client.post(
            "/token",
            data={"grant_type": "client_credentials"},
            auth=(str(client_id), client_secret),
            headers={"User-Agent": "adapter-test"},
        )

    assert response.status_code == 200
    assert response.json() == {"token_type": "Bearer", "access_token": "stored-token"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.anyio
async def test_starlette_adapter_uses_requests_backed_upstream_fetch(
    settings: Settings,
    bridge_harness: BridgeHarness,
    requests_mock: object,
    asgi_client: AsgiClient,
) -> None:
    _ = bridge_harness
    requests_mock.post(  # type: ignore[union-attr] # Pytest fixture protocol is external.
        settings.oauth.token_uri,
        json={"token_type": "Bearer", "access_token": "provider-token"},
    )
    fallback_observer = RecordingFallbackObserver()
    app = create_app(settings, fallback_observer=fallback_observer)

    async with asgi_client(app) as client:
        authorization = await client.get("/")
        state = urllib.parse.parse_qs(
            urllib.parse.urlsplit(authorization.headers["location"]).query
        )["state"][0]
        callback = await client.get(
            "/callback", params={"code": "authorization-code", "state": state}
        )

    assert fallback_observer.failures == []
    assert callback.status_code == HTTPStatus.OK


@pytest.mark.anyio
async def test_starlette_adapter_observes_unknown_application_fault(
    settings: Settings, asgi_client: AsgiClient
) -> None:
    fallback_observer = RecordingFallbackObserver()
    app = create_app(settings, fallback_observer=fallback_observer)

    async def crash(_: Request) -> None:
        raise RuntimeError("unexpected failure")

    app.add_route("/crash", crash)

    async with asgi_client(app, raise_app_exceptions=False) as client:
        response = await client.get("/crash")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.content == b"Internal Server Error"
    assert response.headers["cache-control"] == "no-store"
    assert len(fallback_observer.failures) == 1
    endpoint, failure = fallback_observer.failures[0]
    assert endpoint == types.Endpoint.UNKNOWN
    assert isinstance(failure, RuntimeError)
