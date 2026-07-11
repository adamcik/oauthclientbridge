from oauthclientbridge import (
    create_app,
    logs,
    sentry,
    start_runtime_services,
    telemetry,
)
from oauthclientbridge.settings import Settings

settings = Settings()

logs.init_logging(settings.log)

sentry.init(settings.sentry)

telemetry.instrument()
telemetry.init_tracing(settings.otel)
telemetry.init_metrics(settings.otel)

app = create_app(settings)
start_runtime_services(app)
