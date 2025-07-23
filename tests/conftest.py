import base64
import json
from typing import Any, NamedTuple, Protocol

import pytest
from flask import Flask
from flask.ctx import AppContext
from flask.testing import FlaskClient
from pydantic import SecretStr
from werkzeug.datastructures import Headers

from oauthclientbridge import create_app, crypto, db
from oauthclientbridge.settings import (
    BridgeSettings,
    DatabaseSettings,
    OAuthSettings,
    Settings,
)


class ResponseTuple(NamedTuple):
    data: dict[str, Any]
    status: int
    headers: Headers


class TokenTuple(NamedTuple):
    client_id: str
    client_secret: str
    value: dict[str, Any]


@pytest.fixture
def settings():
    # https://github.com/pydantic/pydantic-settings/issues/201
    return Settings(
        bridge=BridgeSettings(  # pyright: ignore[reportCallIssue]
            secret_key=SecretStr("s3cret"),
            callback_template="{{ variables|tojson }}",
        ),
        database=DatabaseSettings(  # pyright: ignore[reportCallIssue]
            database=":memory:",
        ),
        oauth=OAuthSettings(  # pyright: ignore[reportCallIssue]
            client_id="client",
            client_secret=SecretStr("s3cret"),
            authorization_uri="https://provider.example.com/auth",
            token_uri="https://provider.example.com/token",
            redirect_uri="https://client.example.com/callback",
        ),
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def app_context(app: Flask):
    with app.app_context() as ctx:
        db.initialize()
        yield ctx


@pytest.fixture
def client(app: Flask, app_context: AppContext):
    _ = app_context

    yield app.test_client()


@pytest.fixture
def cursor(app_context: AppContext):
    _ = app_context

    with db.get() as connection:
        yield connection.cursor()


class GetClient(Protocol):
    def __call__(self, path: str) -> ResponseTuple: ...


@pytest.fixture
def get(client: FlaskClient) -> GetClient:
    def _get(path: str):
        resp = client.get(path)
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
        auth: tuple[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> ResponseTuple: ...


@pytest.fixture
def post(client: FlaskClient):
    def _post(
        path: str,
        data: dict[str, Any],
        auth: tuple[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ):
        if not headers:
            headers = {}

        if auth:
            encoded = base64.b64encode(("%s:%s" % auth).encode("ascii"))
            headers["Authorization"] = "Basic %s" % encoded.decode("ascii")

        resp = client.post(path, headers=headers, data=data)

        return ResponseTuple(
            json.loads(resp.text),
            resp.status_code,
            resp.headers,
        )

    return _post


@pytest.fixture
def state(client: FlaskClient):
    with client.session_transaction() as session:
        session["state"] = "abcdef"
    return "abcdef"


@pytest.fixture
def client_state(client: FlaskClient):
    with client.session_transaction() as session:
        session["client_state"] = "s3cret"
    return "s3cret"


def _test_token(**data: str | int):
    client_secret = crypto.generate_key()
    token = crypto.dumps(client_secret, data)
    client_id = db.insert(token)
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
    return _test_token(
        refresh_token="abc",
    )
