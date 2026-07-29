import pytest

from oauthclientbridge import db
from oauthclientbridge.asgi import create_app
from oauthclientbridge.asgi_context import TokenStateRefresher
from oauthclientbridge.settings import Settings


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
