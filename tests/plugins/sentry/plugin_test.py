import importlib
import logging
import sys
from typing import TypeGuard
from unittest.mock import patch

import pytest
import sentry_sdk
from opentelemetry import trace
from pydantic import SecretStr
from pytest_sentry_capture import SentryCapture

from oauthclientbridge import logs
from oauthclientbridge.settings import LogLevel, LogSettings, SentrySettings
from oauthclientbridge.telemetry import _sentry

type SentryEvent = dict[str, object]


@pytest.fixture
def sentry_settings() -> SentrySettings:
    """A pytest fixture that provides SentrySettings."""
    return SentrySettings(
        enabled=True,
        dsn=SecretStr("http://test:test@localhost/1"),
        sample_rate=1.0,
        traces_sample_rate=1.0,
    )


def test_init_sentry_disabled() -> None:
    """If sentry is disabled, we don't initialize."""
    with patch("sentry_sdk.init") as mock_init:
        _sentry.init(SentrySettings(enabled=False), "flask")
        mock_init.assert_not_called()


def test_init_sentry_sdk_installed(sentry_settings: SentrySettings) -> None:
    """If sentry is enabled and installed, we initialize it."""
    assert sentry_settings.dsn is not None

    with patch("sentry_sdk.init") as mock_init:
        _sentry.init(sentry_settings, "flask")

        mock_init.assert_called_once()
        call_kwargs = mock_init.call_args[1]

        assert call_kwargs["dsn"] == sentry_settings.dsn.get_secret_value()
        assert call_kwargs["release"] == sentry_settings.release
        assert call_kwargs["include_local_variables"] is False


def test_trace_sampler_uses_path_overrides(sentry_settings: SentrySettings) -> None:
    sentry_settings.traces_sample_rate_overrides = {"/metrics": 0.1}
    flask_sample = _sentry._traces_sampler(  # pyright: ignore[reportPrivateUsage] # Direct implementation test.
        sentry_settings, "flask"
    )
    starlette_sample = _sentry._traces_sampler(  # pyright: ignore[reportPrivateUsage] # Direct implementation test.
        sentry_settings, "starlette"
    )

    assert flask_sample({"wsgi_environ": {"PATH_INFO": "/metrics"}}) == 0.1
    assert starlette_sample({"asgi_scope": {"path": "/metrics"}}) == 0.1
    assert flask_sample({"wsgi_environ": {"PATH_INFO": "/authorize"}}) == 1.0


def test_init_sentry_sdk_not_installed(
    caplog: pytest.LogCaptureFixture, sentry_settings: SentrySettings
) -> None:
    """If sentry is enabled, but not installed, we log an error."""
    try:
        with patch.dict(sys.modules, {"sentry_sdk": None}):
            _ = importlib.reload(_sentry)
            _sentry.init(sentry_settings, "flask")
            assert (
                "Sentry is enabled, but 'sentry-sdk' is not installed." in caplog.text
            )
    finally:
        _ = importlib.reload(_sentry)


# TODO: Add capture message...
# TODO: Test log exception and log error etc in except


def test_sentry_captures_user_and_tags(sentry_capture: SentryCapture) -> None:
    sentry_sdk.set_user({"id": "user-42", "email": "test@example.com"})
    sentry_sdk.set_tag("transaction_id", "txn-abc-123")

    try:
        raise ValueError("test exception")
    except ValueError:
        sentry_sdk.capture_exception()

    sentry_capture.flush()
    event = next(sentry_capture.get_events())

    assert "user" in event
    assert event["user"]["id"] == "user-42"

    assert "tags" in event
    assert event["tags"]["transaction_id"] == "txn-abc-123"
    frames = event["exception"]["values"][0]["stacktrace"]["frames"]
    assert frames
    assert all("vars" not in frame for frame in frames)


def test_sentry_captures_log_breadcrumbs(sentry_capture: SentryCapture) -> None:
    logs.init_logging(LogSettings(level=LogLevel.DEBUG))

    logger = logging.getLogger(__name__)
    logger.info("Starting the process.")
    logger.warning("Something looks suspicious.")

    try:
        raise ValueError("test exception")
    except ValueError:
        sentry_sdk.capture_exception()

    sentry_capture.flush()
    info_log = sentry_capture.find_breadcrumb_by_level("info")
    assert info_log["message"] == "Starting the process."

    warning_log = sentry_capture.find_breadcrumb_by_level("warning")
    assert warning_log["message"] == "Something looks suspicious."


def test_sentry_captures_chained_exception(sentry_capture: SentryCapture) -> None:
    try:
        try:
            raise ZeroDivisionError
        except ZeroDivisionError as e:
            raise TypeError("Something went wrong") from e
    except TypeError:
        sentry_sdk.capture_exception()

    sentry_capture.flush()
    assert len(list(sentry_capture.get_exceptions())) == 2
    sentry_capture.find_exception_by_type("ZeroDivisionError")
    sentry_capture.find_exception_by_type("TypeError")


def test_sentry_captures_otel_span(
    sentry_capture: SentryCapture, tracer: trace.Tracer
) -> None:
    try:
        with (
            tracer.start_as_current_span("parent-operation"),
            tracer.start_as_current_span("child-operation"),
        ):
            raise ValueError("test exception")
    except ValueError:
        sentry_sdk.capture_exception()

    sentry_capture.flush()
    trace_context = sentry_trace_context(next(sentry_capture.get_events()))
    assert trace_context.get("trace_id") is not None
    assert trace_context.get("span_id") is not None


def sentry_trace_context(event: object) -> SentryEvent:
    assert _is_sentry_event(event)
    contexts = event.get("contexts")
    assert _is_sentry_event(contexts)
    trace_context = contexts.get("trace")
    assert _is_sentry_event(trace_context)
    return trace_context


def _is_sentry_event(value: object) -> TypeGuard[SentryEvent]:
    return isinstance(value, dict)
