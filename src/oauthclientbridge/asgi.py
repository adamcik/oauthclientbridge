from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from anyio import create_task_group
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware

from oauthclientbridge import bridge, logs, oauth, observer, telemetry
from oauthclientbridge.asgi_context import AppContext, TokenStateRefresher
from oauthclientbridge.proxy import ForwardedHeadersMiddleware
from oauthclientbridge.routes import fallback, routes
from oauthclientbridge.settings import Settings


def create_app(
    settings: Settings,
    *,
    fetch: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    fallback_observer: observer.FallbackObserver | None = None,
    outcome_observer: observer.OAuthOutcomeObserver | None = None,
    initialize_runtime: bool = True,
) -> Starlette:
    """Create the in-process Starlette adapter without starting an ASGI runtime."""
    if initialize_runtime:
        logs.init_logging(settings.log)
        telemetry.init_sentry(settings.sentry)
        telemetry.instrument()
        telemetry.init_tracing(settings.otel)
        telemetry.init_metrics(settings.otel)
        telemetry.set_build_info_metric(settings.otel)

    if settings.session_secret is None:
        raise ValueError("BRIDGE_SESSION_SECRET must be set for the ASGI adapter")

    token_state_refresher = TokenStateRefresher(settings.database)
    oauth_bridge = bridge.Bridge(settings, fetch or oauth.fetch_for(settings.fetch))
    fallback_observer = fallback_observer or telemetry.fallback_observer()
    outcome_observer = outcome_observer or telemetry.oauth_outcome_observer()

    @asynccontextmanager
    async def lifespan(_: Starlette):
        async with create_task_group() as task_spawner:
            await token_state_refresher.start(task_spawner)
            yield

    app = Starlette(
        routes=routes,
        exception_handlers={Exception: fallback},
        lifespan=lifespan,
    )
    app.state.context = AppContext(
        settings=settings,
        oauth_bridge=oauth_bridge,
        token_state_refresher=token_state_refresher,
        fallback_observer=fallback_observer,
        outcome_observer=outcome_observer,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
    )
    telemetry.instrument_asgi_app(
        app,
        telemetry.request_lifecycle_observer(settings.log.access_log_format),
    )
    app.add_middleware(ForwardedHeadersMiddleware)
    return app
