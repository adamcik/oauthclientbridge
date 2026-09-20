from http import HTTPStatus

import structlog

from oauthclientbridge import types
from oauthclientbridge.errors import OAuthError

from . import _otel, _prometheus


def bind_log_context(endpoint: types.Endpoint, error: OAuthError | None) -> None:
    values = {"oauth_endpoint": endpoint.value}
    if error is not None:
        values["oauth_error"] = error.value
    structlog.contextvars.bind_contextvars(**values)


class OAuthOutcomeObserver:
    """Records bounded OAuth response outcomes without affecting route behavior."""

    def observe(
        self,
        endpoint: types.Endpoint,
        status: HTTPStatus,
        error: OAuthError | None,
    ) -> None:
        try:
            bind_log_context(endpoint, error)
            _otel.record_oauth_outcome_trace(endpoint.value, status, error)
            if error is not None:
                _prometheus.ServerErrorCounter.labels(
                    endpoint=endpoint.value,
                    status=_prometheus.status(status),
                    error=error.value,
                ).inc()
        except Exception:
            # Observation failures must never change the OAuth response.
            return
