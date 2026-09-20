import atexit

from opentelemetry import trace

from oauthclientbridge import (
    create_app,
    logs,
    oauth,
    start_runtime_services,
    stop_runtime_services,
    telemetry,
)
from oauthclientbridge.settings import Settings

tracer = trace.get_tracer(__name__)

settings = Settings()

logs.init_logging(settings.log)

telemetry.init_sentry(settings.sentry, "flask")

telemetry.instrument()
telemetry.init_tracing(settings.otel)
telemetry.init_metrics(settings.otel)

with tracer.start_as_current_span("STARTUP"):
    app = create_app(settings, fetch=oauth.fetch_with_requests)
    start_runtime_services(app)

atexit.register(stop_runtime_services, app)
