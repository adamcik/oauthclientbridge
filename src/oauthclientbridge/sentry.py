import logging

from oauthclientbridge.settings import SentrySettings

logger = logging.getLogger(__name__)

_initialized = False


def configure_sentry(settings: SentrySettings) -> None:
    if not settings.enabled:
        return

    global _initialized
    if _initialized:
        raise RuntimeError("Sentry should only be initialized once")

    try:
        import sentry_sdk
    except ImportError:
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
        _experiments={"enable_logs": True},
    )

    _initialized = True
