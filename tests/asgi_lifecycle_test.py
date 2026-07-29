import pytest

from oauthclientbridge import db
from oauthclientbridge.asgi import create_app
from oauthclientbridge.asgi_context import TokenStateRefresher
from oauthclientbridge.settings import Settings


def test_asgi_factory_loads_settings_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGE_SESSION_SECRET", "secret")
    monkeypatch.setenv("OAUTH_CLIENT_ID", "client")
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OAUTH_AUTHORIZATION_URI", "https://provider.example.com/auth")
    monkeypatch.setenv("OAUTH_TOKEN_URI", "https://provider.example.com/token")

    app = create_app(initialize_runtime=False)

    assert app.state.context.settings.oauth.client_id == "client"


@pytest.mark.anyio
async def test_asgi_lifespan_requires_initialized_database(settings: Settings) -> None:
    app = create_app(settings, initialize_runtime=False)

    with pytest.raises(RuntimeError, match="Database must be initialized"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.anyio
async def test_asgi_lifespan_starts_token_state_refresh(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = False

    async def start(self: TokenStateRefresher, task_spawner: object) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(db, "is_initialized", lambda _: True)
    monkeypatch.setattr(TokenStateRefresher, "start", start)
    app = create_app(settings, initialize_runtime=False)

    async with app.router.lifespan_context(app):
        assert started is True
