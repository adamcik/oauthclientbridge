"""Establish local diagnostics before reading configuration or starting exporters.

Entry points own the failure boundary. Workers initialize remote telemetry themselves,
so the Uvicorn supervisor never starts exporter threads that workers might inherit.
"""

import traceback
from collections.abc import Generator
from contextlib import contextmanager
from typing import Literal

from pydantic import ValidationError

from oauthclientbridge import logs, settings, telemetry


@contextmanager
def bootstrap(log_settings: settings.LogSettings | None = None) -> Generator[None]:
    """Report startup failures once, without validation inputs or frame locals."""
    # model_construct bypasses environment and credential files: even invalid LOG_*
    # settings must have a working stderr sink before we try to validate them.
    configured_logs = settings.LogSettings.model_construct()
    logs.init_logging(configured_logs)
    try:
        configured_logs = log_settings or settings.LogSettings()
        logs.init_logging(configured_logs)
        yield
    except Exception as error:  # noqa: BLE001 - The process boundary reports any fatal startup failure.
        frames = traceback.extract_tb(error.__traceback__)
        details: dict[str, object] = {"error_type": type(error).__name__}
        if configured_logs.json_output:
            details["frames"] = [
                {"filename": frame.filename, "lineno": frame.lineno, "name": frame.name}
                for frame in frames
            ]
        else:
            details["stack"] = "Startup stack:\n" + "\n".join(
                f"  {frame.filename}:{frame.lineno} in {frame.name}" for frame in frames
            )
        if isinstance(error, ValidationError):
            details["errors"] = [
                # Deeper locations can include user-supplied dictionary keys.
                {"location": item["loc"][:1], "type": item["type"]}
                for item in error.errors(include_input=False, include_context=False)
            ]
        elif isinstance(error, OSError):
            details["errno"] = error.errno
            details["error_filename"] = error.filename
        # Arbitrary exception messages (including SettingsError causes) can contain
        # raw credentials. Do not pass exc_info to logging or Sentry here.
        logs.logger.error("Startup failed", **details)
        raise SystemExit(1) from None


def initialize(
    runtime: Literal["flask", "starlette"],
    configured: settings.Settings | None = None,
) -> settings.Settings:
    """Enable error reporting before application-specific settings are validated."""
    telemetry.init_sentry(
        configured.sentry if configured is not None else settings.SentrySettings(),
        runtime,
    )
    configured = configured if configured is not None else settings.Settings()
    telemetry.instrument()
    telemetry.init_tracing(configured.otel)
    telemetry.init_metrics(configured.otel)
    return configured
