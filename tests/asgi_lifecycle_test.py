import pytest

from oauthclientbridge import db, oauth
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


@pytest.mark.anyio
async def test_asgi_lifespan_closes_only_runtime_owned_httpx_client(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingClient(oauth.HttpxUpstreamClient):
        closed = False

        async def aclose(self) -> None:
            self.closed = True
            await super().aclose()

    monkeypatch.setattr(db, "is_initialized", lambda _: True)
    runtime_owned = RecordingClient(settings.fetch)
    monkeypatch.setattr(oauth, "create_httpx_upstream_client", lambda _: runtime_owned)
    injected = RecordingClient(settings.fetch)

    runtime_app = create_app(settings, initialize_runtime=False)
    injected_app = create_app(settings, fetch=injected.fetch, initialize_runtime=False)

    async with runtime_app.router.lifespan_context(runtime_app):
        pass
    async with injected_app.router.lifespan_context(injected_app):
        pass

    assert runtime_owned.closed is True
    assert injected.closed is False
    await injected.aclose()

    failed_startup = RecordingClient(settings.fetch)
    monkeypatch.setattr(oauth, "create_httpx_upstream_client", lambda _: failed_startup)
    monkeypatch.setattr(db, "is_initialized", lambda _: False)
    failed_app = create_app(settings, initialize_runtime=False)

    with pytest.raises(RuntimeError, match="Database must be initialized"):
        async with failed_app.router.lifespan_context(failed_app):
            pass

    assert failed_startup.closed is True
