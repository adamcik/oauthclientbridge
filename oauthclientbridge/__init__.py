# flake8: noqa

from flask import Flask

app = Flask(__name__)
app.config.from_object('oauthclientbridge.default_settings')
app.config.from_envvar('OAUTH_SETTINGS')

import oauthclientbridge.cli
import oauthclientbridge.logging
import oauthclientbridge.views
