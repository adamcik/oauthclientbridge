import logging
from typing import Any

from oauthclientbridge.settings import SentrySettings

logger = logging.getLogger(__name__)

try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None


def init(settings: SentrySettings) -> None:
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
        integrations=[
            FlaskIntegration(),
            LoggingIntegration(event_level=None, level=None),
        ],
        instrumenter="otel",
        _experiments={"enable_logs": True},
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
