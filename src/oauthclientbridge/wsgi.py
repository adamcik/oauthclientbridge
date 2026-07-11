import atexit

from opentelemetry import trace

from oauthclientbridge import (
    create_app,
    logs,
    sentry,
    start_runtime_services,
    stop_runtime_services,
    telemetry,
)
from oauthclientbridge.settings import Settings

tracer = trace.get_tracer(__name__)

settings = Settings()

logs.init_logging(settings.log)

sentry.init(settings.sentry)

telemetry.instrument()
telemetry.init_tracing(settings.otel)
telemetry.init_metrics(settings.otel)

with tracer.start_as_current_span("STARTUP"):
    app = create_app(settings)
    start_runtime_services(app)

atexit.register(stop_runtime_services, app)
