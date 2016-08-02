# flake8: noqa

from flask import Flask

from werkzeug.contrib.fixers import ProxyFix

app = Flask(__name__)
app.config.from_object('oauthclientbridge.default_settings')
app.config.from_envvar('OAUTH_SETTINGS')

if app.config['OAUTH_NUM_PROXIES']:
    app.wsgi_app = ProxyFix(app.wsgi_app, app.config['OAUTH_NUM_PROXIES'])

import oauthclientbridge.cli
import oauthclientbridge.logging
import oauthclientbridge.views
