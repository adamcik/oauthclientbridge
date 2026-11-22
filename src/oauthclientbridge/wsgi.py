import atexit
from typing import Any

from opentelemetry import trace

from oauthclientbridge import (
    create_app,
    oauth,
    start_runtime_services,
    startup,
    stop_runtime_services,
    types,
)

tracer = trace.get_tracer(__name__)

with startup.bootstrap():
    settings = startup.initialize("flask")
    with tracer.start_as_current_span("STARTUP"):

        async def _fetch(
            uri: str,
            upstream_grant_type: types.UpstreamGrantType,
            auth: str | None = None,
            **data: str | None,
        ) -> dict[str, Any]:
            return await oauth.fetch_with_requests(
                settings.fetch, uri, upstream_grant_type, auth, **data
            )

        app = create_app(settings, fetch=_fetch)
        start_runtime_services(app)

atexit.register(stop_runtime_services, app)
