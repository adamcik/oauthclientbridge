import atexit
from functools import partial

from opentelemetry import trace

from oauthclientbridge import (
    create_app,
    oauth,
    start_runtime_services,
    startup,
    stop_runtime_services,
)

tracer = trace.get_tracer(__name__)

with startup.bootstrap():
    settings = startup.initialize("flask")
    with tracer.start_as_current_span("STARTUP"):
        app = create_app(
            settings, fetch=partial(oauth.fetch_with_requests, settings.fetch)
        )
        start_runtime_services(app)

atexit.register(stop_runtime_services, app)
