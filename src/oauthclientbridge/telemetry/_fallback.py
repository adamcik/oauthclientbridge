from opentelemetry import trace

from oauthclientbridge import types

from . import _sentry


class FallbackObserver:
    """Records an unexpected endpoint fault without changing its response."""

    def observe(self, endpoint: types.Endpoint, exception: BaseException) -> None:
        try:
            span = trace.get_current_span()
            span.set_attribute("error.unhandled", True)
            span.set_attribute("oauth.endpoint", endpoint.value)
            span.record_exception(exception)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            _sentry.capture_unhandled_exception(exception)
        except Exception:
            return
