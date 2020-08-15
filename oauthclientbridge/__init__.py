# flake8: noqa

from flask import Flask

try:
    from werkzeug.middleware.proxy_fix import ProxyFix
except ImportError:
    from werkzeug.contrib.fixers import ProxyFix

__version__ = '1.0.1'

app = Flask(__name__)
app.config.from_object('oauthclientbridge.default_settings')
app.config.from_envvar('OAUTH_SETTINGS', silent=True)

if app.config['OAUTH_NUM_PROXIES']:
    wrapper = ProxyFix(app.wsgi_app, app.config['OAUTH_NUM_PROXIES'])
    app.wsgi_app = wrapper  # type: ignore

try:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration

    if app.config['OAUTH_SENTRY_DSN']:
        sentry_sdk.init(
            dsn=app.config['OAUTH_SENTRY_DSN'],
            integrations=[FlaskIntegration()],
        )
except ImportError as e:
    app.logger.info('Failed to import sentry: %s', e)


import oauthclientbridge.cli
import oauthclientbridge.logging
import oauthclientbridge.views
