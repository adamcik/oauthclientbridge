# pyright: reportImportCycles=none

import importlib.metadata

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

__version__ = importlib.metadata.version("oauthclientbridge")

app = Flask(__name__)
app.config.from_object("oauthclientbridge.default_settings")
_ = app.config.from_envvar("OAUTH_SETTINGS", silent=True)

if app.config["OAUTH_NUM_PROXIES"]:
    wrapper = ProxyFix(
        app.wsgi_app,
        x_for=app.config["OAUTH_NUM_PROXIES"],
    )
    app.wsgi_app = wrapper

import oauthclientbridge.cli  # noqa
import oauthclientbridge.logging  # noqa
import oauthclientbridge.views  # noqa
