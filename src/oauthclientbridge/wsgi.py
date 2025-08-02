from oauthclientbridge import create_app, logs, sentry, telemetry
from oauthclientbridge.settings import Settings

settings = Settings()

logs.init_logging(settings.log)

sentry.init(settings.sentry)

telemetry.instrument()
telemetry.init_tracing(settings.otel)
telemetry.init_metrics(settings.otel)

app = create_app(settings)
logs.init_access_logs(app, settings.log)
