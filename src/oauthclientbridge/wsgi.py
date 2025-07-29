from oauthclientbridge import create_app, logs, sentry, telemetry
from oauthclientbridge.settings import Settings

settings = Settings()

logs.init()
sentry.init(settings.sentry)

telemetry.init_tracing(settings.otel)

app = create_app(settings)
