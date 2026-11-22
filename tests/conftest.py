import logging
import sqlite3
import urllib.parse
from collections.abc import AsyncGenerator, Callable, Generator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple, Protocol, cast
from uuid import uuid4

import httpx
import pytest
import pytest_sentry_capture
import sentry_sdk
import structlog
from pydantic import SecretStr
from starlette.testclient import TestClient

from oauthclientbridge import bridge, crypto, db, oauth, telemetry, types
from oauthclientbridge.asgi import create_app as create_asgi_app
from oauthclientbridge.oauth import (
    _retry as oauth_retry,  # pyright: ignore[reportPrivateUsage] # Global retry limiter reset.
)
from oauthclientbridge.settings import (
    DatabaseSettings,
    OAuthSettings,
    SentrySettings,
    Settings,
)
from tests.oauth_server import OAuthServer

pytest_plugins = ["tests.plugins.otel"]


@pytest.fixture(autouse=True)
def _isolate_sentry(  # pyright: ignore[reportUnusedFunction] # Used by pytest.
    sentry_isolation_scope: None,
) -> None:
    pass


@pytest.fixture
def initialize_sentry(
    sentry_transport: pytest_sentry_capture.FakeTransport,
    sentry_isolation_scope: None,
) -> None:
    """Override SDK setup to exercise our integrations, sampling, and privacy settings.

    The capture plugin owns successful client cleanup; only failed setup needs
    local cleanup because its client-ownership fixture has not started yet.
    """
    try:
        telemetry.init_sentry(
            SentrySettings(
                enabled=True,
                dsn=SecretStr("https://key@example.invalid/1"),
                sample_rate=1.0,
                traces_sample_rate=1.0,
            ),
            "starlette",
            sentry_transport,
        )
    except BaseException:
        sentry_sdk.get_client().close()
        raise


@pytest.fixture(autouse=True)
def reset_logging_handlers():
    """Fixture to reset logging handlers before each test."""
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    structlog.reset_defaults()


@pytest.fixture(autouse=True)
def reset_retry_limiter():
    oauth_retry.get_retry_limiter.cache_clear()


@pytest.fixture
def oauth_server() -> Generator[OAuthServer, None, None]:
    server = OAuthServer()
    try:
        yield server
    finally:
        server.close()


class ResponseTuple(NamedTuple):
    data: dict[str, Any]
    status: int
    headers: httpx.Headers


class TokenTuple(NamedTuple):
    client_id: types.ClientId
    client_secret: types.ClientSecret
    value: dict[str, Any]


@dataclass
class ScriptedOAuth:
    """Strict async upstream seam for direct Bridge tests."""

    results: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str, dict[str, str | None]]] = field(default_factory=list)

    async def fetch(
        self,
        uri: str,
        upstream_grant_type: types.UpstreamGrantType,
        auth: str | None = None,
        **data: str | None,
    ) -> dict[str, Any]:
        if auth is not None:
            data["auth"] = auth
        self.calls.append((uri, upstream_grant_type, data))
        if not self.results:
            raise AssertionError("scripted OAuth received an unexpected request")
        return self.results.pop(0)


@dataclass(frozen=True)
class BridgeHarness:
    bridge: bridge.Bridge
    oauth: ScriptedOAuth
    database: sqlite3.Connection


type AdapterClient = Callable[
    [Settings, oauth.UpstreamFetcher | None],
    AbstractAsyncContextManager[httpx.AsyncClient],
]


@pytest.fixture
def adapter_client(bridge_harness: BridgeHarness) -> AdapterClient:
    _ = bridge_harness  # Keep the shared in-memory database alive for adapter requests.

    @asynccontextmanager
    async def asgi_client(
        settings: Settings, fetch: oauth.UpstreamFetcher | None
    ) -> AsyncGenerator[httpx.AsyncClient]:
        app = create_asgi_app(settings, fetch=fetch, initialize_runtime=False)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://bridge.example.com",
            follow_redirects=False,
        ) as client:
            yield client

    return asgi_client


@pytest.fixture
def scripted_oauth() -> ScriptedOAuth:
    return ScriptedOAuth()


@pytest.fixture
def bridge_harness(
    settings: Settings, scripted_oauth: ScriptedOAuth
) -> Generator[BridgeHarness, None, None]:
    database_uri = f"file:oauthclientbridge-{uuid4()}?mode=memory&cache=shared"
    settings.database.database = database_uri
    database = sqlite3.connect(database_uri, uri=True)
    database.executescript(
        Path("src/oauthclientbridge/schema.sql").read_text(encoding="ascii")
    )
    try:
        yield BridgeHarness(
            bridge.Bridge(settings, scripted_oauth.fetch),
            scripted_oauth,
            database,
        )
    finally:
        database.close()


@pytest.fixture
def settings() -> Settings:
    # https://github.com/pydantic/pydantic-settings/issues/201
    return Settings(
        callback_template="{{ variables|tojson }}",
        session_secret=SecretStr("test-session-secret"),
        database=DatabaseSettings(
            database=":memory:",
        ),
        oauth=OAuthSettings(
            client_id="client",
            client_secret=SecretStr("s3cret"),
            authorization_uri="https://provider.example.com/auth",
            token_uri="https://provider.example.com/token",
            redirect_uri="https://client.example.com/callback",
        ),
    )


@pytest.fixture
def initialized_database(settings: Settings, tmp_path: Path) -> None:
    settings.database.database = str(tmp_path / "tokens.db")
    db.initialize(settings.database)


class GetClient(Protocol):
    def __call__(
        self,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> ResponseTuple: ...


class PostClient(Protocol):
    def __call__(
        self,
        path: str,
        data: dict[str, Any],
        auth: tuple[types.ClientId, types.ClientSecret] | None = None,
        headers: Mapping[str, str | bytes] | None = None,
    ) -> ResponseTuple: ...


@pytest.fixture
def asgi_client(
    settings: Settings, initialized_database: None
) -> Generator[httpx.Client]:
    app = create_asgi_app(
        settings,
        fetch=partial(oauth.fetch_with_requests, settings.fetch),
        initialize_runtime=False,
    )
    # Starlette dynamically selects its client base; this suite uses HTTPX, not HTTPX2.
    client = cast(
        httpx.Client,
        TestClient(
            app,
            base_url="https://bridge.example.com",
            follow_redirects=False,
            raise_server_exceptions=False,
        ),
    )
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def asgi_get(asgi_client: httpx.Client) -> GetClient:
    def get(path: str, headers: dict[str, str] | None = None) -> ResponseTuple:
        response = asgi_client.get(path, headers=headers)
        return ResponseTuple(response.json(), response.status_code, response.headers)

    return get


@pytest.fixture
def asgi_post(asgi_client: httpx.Client) -> PostClient:
    def post(
        path: str,
        data: dict[str, Any],
        auth: tuple[types.ClientId, types.ClientSecret] | None = None,
        headers: Mapping[str, str | bytes] | None = None,
    ) -> ResponseTuple:
        response = asgi_client.post(
            path,
            data={key: str(value) for key, value in data.items()},
            auth=(str(auth[0]), auth[1]) if auth else httpx.USE_CLIENT_DEFAULT,
            headers={
                key: value.decode("ascii") if isinstance(value, bytes) else value
                for key, value in (headers or {}).items()
            },
        )
        return ResponseTuple(response.json(), response.status_code, response.headers)

    return post


@pytest.fixture
def asgi_authorization(asgi_client: httpx.Client) -> tuple[str, str]:
    client_state = "s3cret"
    response = asgi_client.get("/", params={"state": client_state})
    assert response.status_code == 302
    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(response.headers["location"]).query
    )
    return query["state"][0], client_state


@pytest.fixture
def asgi_state(asgi_authorization: tuple[str, str]) -> str:
    return asgi_authorization[0]


@pytest.fixture
def asgi_client_state(asgi_authorization: tuple[str, str]) -> str:
    return asgi_authorization[1]


def _test_token(database: DatabaseSettings, **data: str | int) -> TokenTuple:
    client_secret = crypto.generate_key()
    token = crypto.dumps(client_secret, data)
    client_id = db.generate_id()
    db.insert(client_id, token, database)
    return TokenTuple(client_id, client_secret, data)


@pytest.fixture
def access_token(settings: Settings) -> TokenTuple:
    return _test_token(
        settings.database,
        token_type="test",
        access_token="123",
        expires_in=3600,
    )


@pytest.fixture
def refresh_token(settings: Settings) -> TokenTuple:
    return _test_token(settings.database, refresh_token="abc")
