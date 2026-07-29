"""Starlette-specific adapter translation and fallback tests."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request

from oauthclientbridge import bridge, types
from oauthclientbridge.asgi import create_app
from oauthclientbridge.routes import (
    _response,  # pyright: ignore[reportPrivateUsage] # Adapter session-translation test.
)
from oauthclientbridge.settings import Settings

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
async def test_starlette_adapter_observes_unknown_application_fault(
    settings: Settings, asgi_client: AsgiClient
) -> None:
    fallback_observer = RecordingFallbackObserver()
    app = create_app(
        settings,
        fallback_observer=fallback_observer,
        initialize_runtime=False,
    )

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


@pytest.mark.anyio
async def test_starlette_adapter_scopes_session_cookie_per_instance(
    settings: Settings, asgi_client: AsgiClient
) -> None:
    app = create_app(
        settings.model_copy(
            update={
                "session_cookie_domain": "auth.mopidy.com",
                "session_cookie_path": "/spotify",
            }
        ),
        initialize_runtime=False,
    )

    async with asgi_client(app) as client:
        response = await client.get("/", params={"state": "caller-state"})

    cookie = response.headers["set-cookie"]
    assert "domain=auth.mopidy.com" in cookie
    assert "path=/spotify" in cookie
