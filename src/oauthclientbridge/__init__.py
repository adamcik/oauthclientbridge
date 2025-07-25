# pyright: reportImportCycles=none

from importlib.metadata import version

import structlog
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from oauthclientbridge import logs, sentry
from oauthclientbridge.settings import Settings

__version__ = version("oauthclientbridge")

logs.configure_structlog()

logger: structlog.BoundLogger = structlog.get_logger()
_settings: Settings | None = None


# TODO: Move this to settings module
def get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("Settings not initialized. Call set_settings() first.")
    return _settings


def set_settings(settings: Settings) -> None:
    global _settings
    _settings = settings


def create_app(settings: Settings | None = None) -> Flask:
    if settings is None:
        # https://github.com/pydantic/pydantic-settings/issues/201
        settings = Settings()  # pyright: ignore[reportCallIssue]

    set_settings(settings)

    sentry.configure_sentry(settings.sentry)

    app = Flask(__name__)

    app.config.from_prefixed_env()

    if settings.num_proxies:
        wrapper = ProxyFix(
            app.wsgi_app,
            x_for=int(settings.num_proxies),
        )
        app.wsgi_app = wrapper

    from oauthclientbridge import db, oauth, stats, views

    _ = app.teardown_appcontext(db.close)

    _ = app.after_request(oauth.nocache)
    _ = app.register_error_handler(oauth.Error, oauth.error_handler)
    _ = app.register_error_handler(500, oauth.fallback_error_handler)

    _ = app.before_request(logs.before_request_log_context)
    _ = app.after_request(logs.after_request_log_context)

    _ = app.before_request(stats.record_metrics)
    _ = app.after_request(stats.finalize_metrics)

    app.register_blueprint(views.routes)

    @app.cli.command("initdb")
    def initdb():
        print("Initializing %s" % settings.database.database)
        db.initialize()

    @app.cli.command("cleandb")
    def cleandb():
        print("Vacummed %s" % settings.database.database)
        db.vacuum()

    return app
