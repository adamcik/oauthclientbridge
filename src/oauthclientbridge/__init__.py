# pyright: reportImportCycles=none

from importlib.metadata import version

import structlog
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from oauthclientbridge import db, logs, oauth, stats, telemetry, views
from oauthclientbridge.settings import Settings

__version__ = version("oauthclientbridge")


logger: structlog.BoundLogger = structlog.get_logger()


def create_app(settings: Settings | None = None) -> Flask:
    if settings is None:
        settings = Settings()

    app = Flask(__name__)
    app.config["SETTINGS"] = settings
    _ = app.config.from_prefixed_env()

    telemetry.instrument_app(app)

    forwarded_for_proxies = settings.forwarded_for_proxies
    if forwarded_for_proxies is None:
        forwarded_for_proxies = settings.num_proxies

    if any(
        (
            forwarded_for_proxies,
            settings.forwarded_proto_proxies,
            settings.forwarded_host_proxies,
            settings.forwarded_port_proxies,
        )
    ):
        wrapper = ProxyFix(
            app.wsgi_app,
            x_for=forwarded_for_proxies,
            x_proto=settings.forwarded_proto_proxies,
            x_host=settings.forwarded_host_proxies,
            x_port=settings.forwarded_port_proxies,
        )
        app.wsgi_app = wrapper

    logs.init_access_logs(settings.log, app)

    _ = app.teardown_appcontext(db.close)

    _ = app.after_request(oauth.nocache)
    _ = app.register_error_handler(oauth.Error, oauth.error_handler)
    _ = app.register_error_handler(500, oauth.fallback_error_handler)

    _ = app.before_request(stats.record_metrics)
    _ = app.after_request(stats.finalize_metrics)

    stats.set_build_info(settings.otel)
    stats.add_refresher(
        app,
        lambda: stats.set_token_state_counts(db.token_state_counts()),
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

    stats.start_background_refresh(app)
    stats.request_refresh(app)
    app.extensions["oauth_runtime_services_started"] = True


def stop_runtime_services(app: Flask) -> None:
    stats.stop_background_refresh(app)
    app.extensions.pop("oauth_runtime_services_started", None)
