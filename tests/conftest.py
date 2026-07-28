import base64
import json
import logging
import sqlite3
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Protocol
from uuid import uuid4

import pytest
import structlog
from flask import Flask
from flask.ctx import AppContext
from flask.testing import FlaskClient
from pydantic import SecretStr
from werkzeug.datastructures import Headers

from oauthclientbridge import bridge, create_app, crypto, db, types
from oauthclientbridge.oauth import (
    _retry as oauth_retry,  # pyright: ignore[reportPrivateUsage] # Global retry limiter reset.
)
from oauthclientbridge.settings import (
    DatabaseSettings,
    OAuthSettings,
    Settings,
)

pytest_plugins = ["tests.plugins.sentry", "tests.plugins.otel"]


@pytest.fixture(autouse=True)
def _isolate_sentry(  # pyright: ignore[reportUnusedFunction] # Used by pytest.
    sentry_isolation_scope: None,
) -> None:
    pass


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


class ResponseTuple(NamedTuple):
    data: dict[str, Any]
    status: int
    headers: Headers


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
        self, uri: str, endpoint: str, **data: str | None
    ) -> dict[str, Any]:
        self.calls.append((uri, endpoint, data))
        if not self.results:
            raise AssertionError("scripted OAuth received an unexpected request")
        return self.results.pop(0)


@dataclass(frozen=True)
class BridgeHarness:
    bridge: bridge.Bridge
    oauth: ScriptedOAuth
    database: sqlite3.Connection


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
def app(settings: Settings) -> Flask:
    app = create_app(settings)
    app.secret_key = "test-secret-key"
    return app


@pytest.fixture(scope="function")
def app_context(app: Flask, settings: Settings) -> Generator[AppContext, None, None]:
    with app.app_context() as ctx:
        db.initialize()
        yield ctx


@pytest.fixture
def client(app: Flask, app_context: AppContext) -> Generator[FlaskClient, None, None]:
    _ = app_context

    yield app.test_client()


@pytest.fixture
def cursor(app_context: AppContext) -> Generator[sqlite3.Cursor, None, None]:
    _ = app_context

    with db.get() as connection:
        yield connection.cursor()


class GetClient(Protocol):
    def __call__(
        self,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> ResponseTuple: ...


@pytest.fixture
def get(client: FlaskClient) -> GetClient:
    def _get(path: str, headers: dict[str, str] | None = None) -> ResponseTuple:
        resp = client.get(path, headers=headers)
        return ResponseTuple(
            json.loads(resp.text),
            resp.status_code,
            resp.headers,
        )

    return _get


class PostClient(Protocol):
    def __call__(
        self,
        path: str,
        data: dict[str, Any],
        auth: tuple[types.ClientId, types.ClientSecret] | None = None,
        headers: Mapping[str, str | bytes] | None = None,
    ) -> ResponseTuple: ...


@pytest.fixture
def post(client: FlaskClient) -> PostClient:
    def _post(
        path: str,
        data: dict[str, Any],
        auth: tuple[types.ClientId, types.ClientSecret] | None = None,
        headers: Mapping[str, str | bytes] | None = None,
    ) -> ResponseTuple:
        request_headers = dict(headers or {})

        if auth:
            encoded = base64.b64encode(("%s:%s" % auth).encode("ascii"))
            request_headers["Authorization"] = "Basic %s" % encoded.decode("ascii")

        resp = client.post(path, headers=request_headers, data=data)

        return ResponseTuple(
            json.loads(resp.text),
            resp.status_code,
            resp.headers,
        )

    return _post


@pytest.fixture
def state(client: FlaskClient) -> str:
    """Populate the OAuth state used by callback-flow tests."""
    with client.session_transaction() as session:
        session["state"] = "abcdef"
    return "abcdef"


@pytest.fixture
def client_state(client: FlaskClient) -> str:
    """Populate client-provided state stored alongside OAuth state."""
    with client.session_transaction() as session:
        session["client_state"] = "s3cret"
    return "s3cret"


def _test_token(**data: str | int) -> TokenTuple:
    client_secret = crypto.generate_key()
    token = crypto.dumps(client_secret, data)
    client_id = db.generate_id()
    db.insert(client_id, token)
    return TokenTuple(client_id, client_secret, data)


@pytest.fixture
def access_token() -> TokenTuple:
    return _test_token(
        token_type="test",
        access_token="123",
        expires_in=3600,
    )


@pytest.fixture
def refresh_token() -> TokenTuple:
    return _test_token(refresh_token="abc")
