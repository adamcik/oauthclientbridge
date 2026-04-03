# pyright: reportImportCycles=none

from importlib.metadata import version

import structlog
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from oauthclientbridge import db, logs, oauth, stats, telemetry, views
from oauthclientbridge.settings import Settings

__version__ = version("oauthclientbridge")


logger: structlog.BoundLogger = structlog.get_logger()


def create_app(settings: Settings) -> Flask:
    app = Flask(__name__)
    app.config["SETTINGS"] = settings
    _ = app.config.from_prefixed_env()

    telemetry.instrument_app(app)

    if settings.num_proxies:
        wrapper = ProxyFix(
            app.wsgi_app,
            x_for=int(settings.num_proxies),
        )
        app.wsgi_app = wrapper

    logs.init_access_logs(settings.log, app)

    _ = app.teardown_appcontext(db.close)

    _ = app.after_request(oauth.nocache)
    _ = app.register_error_handler(oauth.Error, oauth.error_handler)
    _ = app.register_error_handler(500, oauth.fallback_error_handler)

    _ = app.before_request(stats.record_metrics)
    _ = app.after_request(stats.finalize_metrics)

    app.register_blueprint(views.routes)

    @app.cli.command("initdb")
    def initdb():  # pyright: ignore[reportUnusedFunction]
        print("Initializing %s" % settings.database.database)
        db.initialize()

    @app.cli.command("cleandb")
    def cleandb():  # pyright: ignore[reportUnusedFunction]
        print("Vacuumed %s" % settings.database.database)
        db.vacuum()

    return app
