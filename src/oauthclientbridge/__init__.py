# pyright: reportImportCycles=none

from importlib.metadata import version

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from oauthclientbridge.settings import Settings

__version__ = version("oauthclientbridge")

_settings: Settings | None = None


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

    app = Flask(__name__)

    app.secret_key = settings.secret_key.get_secret_value()

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

    _ = app.before_request(stats.record_metrics)
    _ = app.after_request(stats.finalize_metrics)

    app.register_blueprint(views.routes)

    return app
