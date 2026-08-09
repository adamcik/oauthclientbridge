"""Starlette-specific adapter translation and fallback tests."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request

from oauthclientbridge import bridge, types
from oauthclientbridge.asgi import create_app
from oauthclientbridge.routes import (
    _response,  # pyright: ignore[reportPrivateUsage] # Adapter session-translation test.
)
from oauthclientbridge.settings import FetchSettings, Settings
from pytest_otel_capture import OTelMocker
from tests.oauth_server import OAuthServer

type AsgiClient = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


@dataclass
class RecordingFallbackObserver:
    failures: list[tuple[types.Endpoint, BaseException]] = field(default_factory=list)

    def observe(self, endpoint: types.Endpoint, exception: BaseException) -> None:
        self.failures.append((endpoint, exception))


@dataclass(frozen=True)
class RetryConnectionCase:
    name: str
    failure_status: HTTPStatus
    uses_fresh_connection: bool


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


async def _callback(
    client: httpx.AsyncClient,
) -> httpx.Response:
    authorization = await client.get("/")
    state = parse_qs(urlsplit(authorization.headers["location"]).query)["state"][0]
    return await client.get(
        "/callback", params={"code": "authorization-code", "state": state}
    )


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


@pytest.mark.anyio
async def test_asgi_callback_reuses_its_upstream_connection(
    settings: Settings, bridge_harness: object
) -> None:
    _ = bridge_harness  # Keeps the initialized shared-memory database alive.

    class OAuthHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        connections: set[tuple[str, int]] = set()
        response_status = HTTPStatus.OK

        def do_POST(self) -> None:  # noqa: N802 # Required by BaseHTTPRequestHandler.
            self.connections.add(self.client_address)
            length = int(self.headers["Content-Length"])
            self.rfile.read(length)
            body = b'{"access_token":"provider-token","token_type":"Bearer"}'
            self.send_response(self.response_status)
            if self.response_status.is_redirection:
                self.send_header("Location", "http://redirected.example.com/token")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), OAuthHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    settings.oauth.token_uri = f"http://{host}:{port}/token"

    try:
        app = create_app(settings, initialize_runtime=False)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://bridge.example.com",
                follow_redirects=False,
            ) as client:
                for _ in range(2):
                    authorization = await client.get("/")
                    state = parse_qs(urlsplit(authorization.headers["location"]).query)[
                        "state"
                    ][0]
                    callback = await client.get(
                        "/callback",
                        params={"code": "authorization-code", "state": state},
                    )
                    assert callback.status_code == HTTPStatus.OK

                OAuthHandler.response_status = HTTPStatus.FOUND
                authorization = await client.get("/")
                state = parse_qs(urlsplit(authorization.headers["location"]).query)[
                    "state"
                ][0]
                callback = await client.get(
                    "/callback",
                    params={"code": "authorization-code", "state": state},
                )
                assert callback.status_code == HTTPStatus.BAD_REQUEST
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()

    assert len(OAuthHandler.connections) == 1


@pytest.mark.anyio
async def test_asgi_callback_returns_initial_failure_when_retry_budget_is_exhausted(
    settings: Settings,
    bridge_harness: object,
    asgi_client: AsgiClient,
    oauth_server: OAuthServer,
    otel_mock: OTelMocker,
) -> None:
    _ = bridge_harness  # Keeps the initialized shared-memory database alive.
    settings.oauth.token_uri = oauth_server.url_for("/token")
    settings.fetch = FetchSettings(
        total_attempts=2, backoff_factor=0, retry_budget_capacity=0
    )
    oauth_server.expect("/token").respond(status=HTTPStatus.SERVICE_UNAVAILABLE)
    app = create_app(settings, initialize_runtime=False)

    async with app.router.lifespan_context(app):
        async with asgi_client(app) as client:
            response = await _callback(client)
            metrics = await client.get("/metrics")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["error"] == "temporarily_unavailable"
    assert response.status_code != HTTPStatus.TOO_MANY_REQUESTS
    assert len(oauth_server.requests) == 1
    assert (
        b'oauth_client_retry_decisions_total{decision="skip",endpoint="authorization_code",reason="budget_exhausted"} 1.0'
        in metrics.content
    )
    suppressed = [
        event
        for span in otel_mock.get_finished_spans()
        for event in span.events
        if event.name == "Retry suppressed"
    ]
    assert len(suppressed) == 1
    assert suppressed[0].attributes == {
        "oauth.upstream_grant_type": "authorization_code",
        "retry.reason": "budget_exhausted",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        RetryConnectionCase(
            name="gateway failure",
            failure_status=HTTPStatus.SERVICE_UNAVAILABLE,
            uses_fresh_connection=True,
        ),
        RetryConnectionCase(
            name="upstream rate limit",
            failure_status=HTTPStatus.TOO_MANY_REQUESTS,
            uses_fresh_connection=False,
        ),
    ],
    ids=lambda case: case.name,
)
async def test_asgi_callback_retry_connection_and_generation_observability(
    case: RetryConnectionCase,
    settings: Settings,
    bridge_harness: object,
    asgi_client: AsgiClient,
    oauth_server: OAuthServer,
    otel_mock: OTelMocker,
) -> None:
    _ = bridge_harness  # Keeps the initialized shared-memory database alive.
    settings.oauth.token_uri = oauth_server.url_for("/token")
    settings.fetch = FetchSettings(total_attempts=2, backoff_factor=0)
    oauth_server.expect("/token").respond(status=case.failure_status)
    oauth_server.expect("/token").respond(
        {"access_token": "provider-token", "token_type": "Bearer"}
    )
    app = create_app(settings, initialize_runtime=False)

    async with app.router.lifespan_context(app):
        async with asgi_client(app) as client:
            response = await _callback(client)
            metrics = await client.get("/metrics")

    assert response.status_code == HTTPStatus.OK
    connections = {request.client_address for request in oauth_server.requests}
    assert (len(connections) == 2) is case.uses_fresh_connection
    generation_resets = [
        event
        for span in otel_mock.get_finished_spans()
        for event in span.events
        if event.name == "Client generation reset"
    ]
    assert len(generation_resets) == int(case.uses_fresh_connection)
    if case.uses_fresh_connection:
        assert generation_resets[0].attributes == {
            "oauth.upstream_grant_type": "authorization_code",
            "client.reset_error": "http_503",
        }
        assert (
            b'oauth_client_generation_resets_total{endpoint="authorization_code",error="http_503"} 1.0'
            in metrics.content
        )
        assert b"oauth_client_generation_drain_seconds_count 1.0" in metrics.content
