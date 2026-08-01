# pyright: reportImportCycles=none

from importlib.metadata import version
from typing import cast

import structlog
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from oauthclientbridge import (
    bridge,
    db,
    logs,
    oauth,
    telemetry,
    views,
)
from oauthclientbridge.settings import Settings

__version__ = version("oauthclientbridge")


logger: structlog.BoundLogger = structlog.get_logger()


def create_app(
    settings: Settings | None = None, *, fetch: oauth.UpstreamFetcher
) -> Flask:
    if settings is None:
        settings = Settings()

    app = Flask(__name__)
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_host=1,
        x_port=1,
        x_proto=1,
    )
    app.config["SETTINGS"] = settings
    _ = app.config.from_prefixed_env()
    session_secret = settings.session_secret
    if session_secret is None:
        legacy_secret = cast(object, app.config["SECRET_KEY"])
        if not isinstance(legacy_secret, str) or not legacy_secret:
            raise ValueError("BRIDGE_SESSION_SECRET or FLASK_SECRET_KEY must be set")
        app.secret_key = legacy_secret
    else:
        app.secret_key = session_secret.get_secret_value()
    app.config["SESSION_COOKIE_SECURE"] = settings.session_cookie_secure

    outcome_observer = telemetry.oauth_outcome_observer()
    oauth_bridge = bridge.Bridge(settings, fetch)
    app.extensions["oauth_bridge"] = oauth_bridge
    app.extensions["oauth_fallback_observer"] = telemetry.fallback_observer()
    app.extensions["oauth_outcome_observer"] = outcome_observer

    telemetry.instrument_app(app)

    logs.init_access_logs(settings.log, app)

    _ = app.teardown_appcontext(db.close)

    _ = app.register_error_handler(500, views.fallback_error_handler)

    telemetry.set_build_info_metric(settings.otel)
    telemetry.add_refresher(
        app,
        lambda: telemetry.set_token_state_counts_metric(db.token_state_counts()),
    )

    app.register_blueprint(views.routes)

    @app.cli.command("initdb")
    def initdb():  # pyright: ignore[reportUnusedFunction]
        print("Initializing %s" % settings.database.database)
        db.initialize()

    @app.cli.command("upgradedb")
    def upgradedb():  # pyright: ignore[reportUnusedFunction]
        print("Upgrading %s" % settings.database.database)
        db.upgrade()

    @app.cli.command("cleandb")
    def cleandb():  # pyright: ignore[reportUnusedFunction]
        print("Vacuumed %s" % settings.database.database)
        db.vacuum()

    return app


def start_runtime_services(app: Flask) -> None:
    if app.extensions.get("oauth_runtime_services_started") is True:
        return

    with app.app_context():
        if not db.is_initialized():
            raise RuntimeError(
                "Database must be initialized before starting runtime services"
            )

    telemetry.start_background_refresh(app)
    telemetry.request_refresh(app)
    app.extensions["oauth_runtime_services_started"] = True


def stop_runtime_services(app: Flask) -> None:
    telemetry.stop_background_refresh(app)
    app.extensions.pop("oauth_runtime_services_started", None)
