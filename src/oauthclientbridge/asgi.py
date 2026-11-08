from contextlib import asynccontextmanager

from anyio import create_task_group, to_thread
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware

from oauthclientbridge import bridge, db, oauth, observer, startup, telemetry
from oauthclientbridge.asgi_context import AppContext, TokenStateRefresher
from oauthclientbridge.proxy import ForwardedHeadersMiddleware
from oauthclientbridge.routes import fallback, routes
from oauthclientbridge.settings import Settings


def create_app(
    settings: Settings | None = None,
    *,
    fetch: oauth.UpstreamFetcher | None = None,
    fallback_observer: observer.FallbackObserver | None = None,
    outcome_observer: observer.OAuthOutcomeObserver | None = None,
    initialize_runtime: bool = True,
) -> Starlette:
    """Create the ASGI application for an in-process client or ASGI server."""
    if initialize_runtime:
        with startup.bootstrap(settings.log if settings is not None else None):
            settings = startup.initialize("starlette", settings)
            telemetry.set_build_info_metric(settings.otel)
            return _create_app(settings, fetch, fallback_observer, outcome_observer)

    return _create_app(
        settings if settings is not None else Settings(),
        fetch,
        fallback_observer,
        outcome_observer,
    )


def _create_app(
    settings: Settings,
    fetch: oauth.UpstreamFetcher | None,
    fallback_observer: observer.FallbackObserver | None,
    outcome_observer: observer.OAuthOutcomeObserver | None,
) -> Starlette:
    if settings.session_secret is None:
        raise ValueError("BRIDGE_SESSION_SECRET must be set for the ASGI adapter")

    token_state_refresher = TokenStateRefresher(settings.database)
    client: oauth.HttpxUpstreamClient | None = None
    if fetch is None:
        client = oauth.create_httpx_upstream_client(settings.fetch)
        oauth_fetch = client.fetch
    else:
        oauth_fetch = fetch
    oauth_bridge = bridge.Bridge(settings, oauth_fetch)
    fallback_observer = fallback_observer or telemetry.fallback_observer()
    outcome_observer = outcome_observer or telemetry.oauth_outcome_observer()

    @asynccontextmanager
    async def lifespan(_: Starlette):
        try:
            if not await to_thread.run_sync(db.is_initialized, settings.database):
                raise RuntimeError(
                    "Database must be initialized before starting runtime services"
                )
            async with create_task_group() as group:
                await token_state_refresher.start(group)
                telemetry.start_asyncio_monitor(group, "main")
                try:
                    yield
                finally:
                    group.cancel_scope.cancel()
        finally:
            if client is not None:
                await client.aclose()

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
        max_age=None,
        https_only=settings.session_cookie_secure,
        domain=settings.session_cookie_domain,
        path=settings.session_cookie_path,
    )
    telemetry.instrument_asgi_app(
        app,
        telemetry.request_lifecycle_observer(settings.log.access_log_format),
    )
    app.add_middleware(ForwardedHeadersMiddleware)
    return app
