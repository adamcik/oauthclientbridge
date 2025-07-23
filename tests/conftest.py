import base64
import json
from typing import Any, NamedTuple, Protocol

import pytest
from flask import Flask
from flask.ctx import AppContext
from flask.testing import FlaskClient

from oauthclientbridge import create_app, crypto, db


class TestResponse(NamedTuple):
    data: dict[str, Any]
    status: int
    headers: dict[str, str]


class TestToken(NamedTuple):
    client_id: str
    client_secret: str
    value: dict[str, Any]


@pytest.fixture
def app():
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "s3cret",
            "OAUTH_DATABASE": ":memory:",
            "OAUTH_CLIENT_ID": "client",
            "OAUTH_CLIENT_SECRET": "s3cret",
            "OAUTH_AUTHORIZATION_URI": "https://provider.example.com/auth",
            "OAUTH_TOKEN_URI": "https://provider.example.com/token",
            "OAUTH_REDIRECT_URI": "https://client.example.com/callback",
            "OAUTH_CALLBACK_TEMPLATE": "{{ variables|tojson }}",
        }
    )


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
    def __call__(self, path: str) -> TestResponse: ...


@pytest.fixture
def get(client: FlaskClient) -> GetClient:
    def _get(path: str):
        resp = client.get(path)
        return TestResponse(
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
    ) -> TestResponse: ...


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

        return TestResponse(
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
    return TestToken(client_id, client_secret, data)


@pytest.fixture
def access_token() -> TestToken:
    return _test_token(
        token_type="test",
        access_token="123",
        expires_in=3600,
    )


@pytest.fixture
def refresh_token() -> TestToken:
    return _test_token(
        refresh_token="abc",
    )
