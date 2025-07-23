# pyright: reportImportCycles=none

from importlib.metadata import version
from typing import Any

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

__version__ = version("oauthclientbridge")


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object("oauthclientbridge.default_settings")

    if test_config:
        app.config.from_mapping(test_config)
    else:
        _ = app.config.from_envvar("OAUTH_SETTINGS", silent=True)

    if app.config["OAUTH_NUM_PROXIES"]:
        wrapper = ProxyFix(
            app.wsgi_app,
            x_for=app.config["OAUTH_NUM_PROXIES"],
        )
        app.wsgi_app = wrapper

    from oauthclientbridge import db, logging, oauth, stats, views

    with app.app_context():
        app.teardown_appcontext(db.close)
        app.after_request(oauth.nocache)
        app.register_error_handler(oauth.Error, oauth.error_handler)
        app.register_error_handler(500, oauth.fallback_error_handler)
        app.before_request(stats.record_metrics)
        app.after_request(stats.finalize_metrics)
        app.register_blueprint(views.routes)
        logging.configure(app)

    return app
