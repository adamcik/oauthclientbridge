import logging
from collections.abc import Callable, Mapping
from typing import Any

from oauthclientbridge.settings import SentrySettings

logger = logging.getLogger(__name__)


def _traces_sampler(
    settings: SentrySettings,
) -> Callable[[dict[str, object]], float]:
    def sample(sampling_context: dict[str, object]) -> float:
        wsgi_environ = sampling_context.get("wsgi_environ")
        path = (
            wsgi_environ.get("PATH_INFO") if isinstance(wsgi_environ, Mapping) else None
        )
        if not isinstance(path, str):
            return settings.traces_sample_rate
        return settings.traces_sample_rate_overrides.get(
            path, settings.traces_sample_rate
        )

    return sample


try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None


def init(
    settings: SentrySettings,
    transport=None,
) -> None:
    if not settings.enabled:
        return

    if sentry_sdk is None:
        logger.error(
            "Sentry is enabled, but 'sentry-sdk' is not installed. "
            "Please install it with 'pip install oauthclientbridge[sentry]'."
        )
        return

    from sentry_sdk.integrations.flask import FlaskIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=settings.dsn.get_secret_value() if settings.dsn else None,
        sample_rate=settings.sample_rate,
        traces_sample_rate=settings.traces_sample_rate,
        traces_sampler=_traces_sampler(settings),
        integrations=[
            FlaskIntegration(),
            LoggingIntegration(event_level=None, level=None),
        ],
        instrumenter="otel",
        transport=transport,
    )


def set_tag(key: str, value: str) -> None:
    if sentry_sdk:
        sentry_sdk.set_tag(key, value)


def set_tags(tags: dict[str, Any]) -> None:
    if sentry_sdk:
        sentry_sdk.set_tags(tags)


def set_user(user_data: dict[str, Any] | None) -> None:
    if sentry_sdk:
        sentry_sdk.set_user(user_data)
