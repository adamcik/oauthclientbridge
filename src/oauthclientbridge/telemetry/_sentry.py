from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal, TypeGuard

from oauthclientbridge.settings import SentrySettings

if TYPE_CHECKING:
    from sentry_sdk.transport import Transport
    from sentry_sdk.types import Event

    SentryTransport = Transport | Callable[[Event], None] | None
else:
    SentryTransport = object

logger = logging.getLogger(__name__)
type Runtime = Literal["flask", "starlette"]


def _traces_sampler(
    settings: SentrySettings,
    runtime: Runtime,
) -> Callable[[dict[str, object]], float]:
    def sample(sampling_context: dict[str, object]) -> float:
        context_key, path_key = (
            ("wsgi_environ", "PATH_INFO")
            if runtime == "flask"
            else ("asgi_scope", "path")
        )
        context = sampling_context.get(context_key)
        path = context.get(path_key) if _is_mapping(context) else None
        if not isinstance(path, str):
            return settings.traces_sample_rate
        return settings.traces_sample_rate_overrides.get(
            path, settings.traces_sample_rate
        )

    return sample


def _is_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping)


sentry_sdk: ModuleType | None
try:
    import sentry_sdk as _sentry_sdk
except ImportError:
    sentry_sdk = None
else:
    sentry_sdk = _sentry_sdk


def init(
    settings: SentrySettings,
    runtime: Runtime,
    transport: SentryTransport = None,
) -> None:
    if not settings.enabled:
        return

    if sentry_sdk is None:
        logger.error(
            "Sentry is enabled, but 'sentry-sdk' is not installed. "
            "Please install it with 'pip install oauthclientbridge[sentry]'."
        )
        return

    from sentry_sdk.integrations.logging import LoggingIntegration

    if runtime == "flask":
        from sentry_sdk.integrations.flask import FlaskIntegration

        runtime_integration: object = FlaskIntegration()
    else:
        from sentry_sdk.integrations.starlette import StarletteIntegration

        runtime_integration = StarletteIntegration()

    sentry_sdk.init(
        dsn=settings.dsn.get_secret_value() if settings.dsn else None,
        release=settings.release,
        sample_rate=settings.sample_rate,
        traces_sample_rate=settings.traces_sample_rate,
        traces_sampler=_traces_sampler(settings, runtime),
        integrations=[
            runtime_integration,
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


def capture_unhandled_exception(exception: BaseException) -> None:
    if sentry_sdk:
        sentry_sdk.capture_exception(exception)
