from collections.abc import Awaitable, Callable
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware

from oauthclientbridge import bridge, oauth, observer, telemetry
from oauthclientbridge.asgi_context import AppContext
from oauthclientbridge.routes import routes
from oauthclientbridge.settings import Settings

def create_app(
    settings: Settings,
    *,
    fetch: Callable[..., Awaitable[dict[str, Any]]] = oauth.fetch,
    fallback_observer: observer.FallbackObserver | None = None,
    outcome_observer: observer.OAuthOutcomeObserver | None = None,
) -> Starlette:
    """Create the in-process Starlette adapter without starting an ASGI runtime."""
    if settings.session_secret is None:
        raise ValueError("BRIDGE_SESSION_SECRET must be set for the ASGI adapter")

    oauth_bridge = bridge.Bridge(settings, fetch)
    fallback_observer = fallback_observer or telemetry.fallback_observer()
    outcome_observer = outcome_observer or telemetry.oauth_outcome_observer()

    app = Starlette(routes=routes)
    app.state.context = AppContext(
        settings=settings,
        oauth_bridge=oauth_bridge,
        fallback_observer=fallback_observer,
        outcome_observer=outcome_observer,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
    )
    return app
