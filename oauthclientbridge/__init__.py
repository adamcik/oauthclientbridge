# flake8: noqa

import importlib.metadata

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

__version__ = importlib.metadata.version("oauthclientbridge")

app = Flask(__name__)
app.config.from_object("oauthclientbridge.default_settings")
app.config.from_envvar("OAUTH_SETTINGS", silent=True)

if app.config["OAUTH_NUM_PROXIES"]:
    wrapper = ProxyFix(app.wsgi_app, app.config["OAUTH_NUM_PROXIES"])
    app.wsgi_app = wrapper  # type: ignore

import oauthclientbridge.cli
import oauthclientbridge.logging
import oauthclientbridge.views
