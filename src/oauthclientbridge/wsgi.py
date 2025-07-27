from oauthclientbridge import create_app, logs, sentry
from oauthclientbridge.settings import Settings

settings = Settings()

logs.init()
sentry.init(settings.sentry)

app = create_app(settings)
