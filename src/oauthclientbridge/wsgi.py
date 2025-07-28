from oauthclientbridge import create_app, logs, sentry, telemetry
from oauthclientbridge.settings import Settings

settings = Settings()

logs.init()
sentry.init(settings.sentry, otel_enabled=settings.otel.enabled)
telemetry.init(settings.otel, sentry_enabled=settings.sentry.enabled)

app = create_app(settings)
