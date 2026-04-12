import importlib
import logging
import sys
from unittest.mock import patch

import pytest
import sentry_sdk
from pydantic import SecretStr

from oauthclientbridge import logs, sentry
from oauthclientbridge.settings import LogLevel, LogSettings, SentrySettings

from . import FakeTransport, SentryCapture


@pytest.fixture(autouse=True)
def _isolate_sentry(sentry_isolation_scope):
    pass


@pytest.fixture
def sentry_settings() -> SentrySettings:
    """A pytest fixture that provides SentrySettings."""
    return SentrySettings(
        enabled=True,
        dsn=SecretStr("http://test:test@localhost/1"),
        sample_rate=1.0,
        traces_sample_rate=1.0,
    )


@pytest.fixture
def capsentry(
    sentry_transport: FakeTransport,
    sentry_settings: SentrySettings,
    sentry_capture,
):
    sentry.init(sentry_settings, sentry_transport)
    return sentry_capture


def test_init_sentry_disabled() -> None:
    """If sentry is disabled, we don't initialize."""
    with patch("sentry_sdk.init") as mock_init:
        sentry.init(SentrySettings(enabled=False))
        mock_init.assert_not_called()


def test_init_sentry_sdk_installed(sentry_settings: SentrySettings) -> None:
    """If sentry is enabled and installed, we initialize it."""
    assert sentry_settings.dsn is not None

    with patch("sentry_sdk.init") as mock_init:
        sentry.init(sentry_settings)

        mock_init.assert_called_once()
        call_kwargs = mock_init.call_args[1]

        assert call_kwargs["dsn"] == sentry_settings.dsn.get_secret_value()


def test_init_sentry_sdk_not_installed(
    caplog: pytest.LogCaptureFixture, sentry_settings: SentrySettings
) -> None:
    """If sentry is enabled, but not installed, we log an error."""
    try:
        with patch.dict(sys.modules, {"sentry_sdk": None}):
            _ = importlib.reload(sentry)
            sentry.init(sentry_settings)
            assert (
                "Sentry is enabled, but 'sentry-sdk' is not installed." in caplog.text
            )
    finally:
        _ = importlib.reload(sentry)


# TODO: Add capture message...
# TODO: Test log exception and log error etc in except


def test_sentry_captures_user_and_tags(capsentry: SentryCapture) -> None:
    sentry_sdk.set_user({"id": "user-42", "email": "test@example.com"})
    sentry_sdk.set_tag("transaction_id", "txn-abc-123")

    try:
        raise ValueError("test exception")
    except ValueError:
        sentry_sdk.capture_exception()

    event = next(capsentry.get_events())

    assert "user" in event
    assert event["user"]["id"] == "user-42"

    assert "tags" in event
    assert event["tags"]["transaction_id"] == "txn-abc-123"


def test_sentry_captures_log_breadcrumbs(capsentry: SentryCapture) -> None:
    logs.init_logging(LogSettings(level=LogLevel.DEBUG))

    logging.info("Starting the process.")
    logging.warning("Something looks suspicious.")

    try:
        raise ValueError("test exception")
    except ValueError:
        sentry_sdk.capture_exception()

    info_log = capsentry.find_breadcrumb_by_level("info")
    assert info_log["message"] == "Starting the process."

    warning_log = capsentry.find_breadcrumb_by_level("warning")
    assert warning_log["message"] == "Something looks suspicious."


def test_sentry_captures_chained_exception(capsentry: SentryCapture) -> None:
    try:
        try:
            1 / 0
        except ZeroDivisionError as e:
            raise TypeError("Something went wrong") from e
    except TypeError:
        sentry_sdk.capture_exception()

    assert len(list(capsentry.get_exceptions())) == 2
    capsentry.find_exception_by_type("ZeroDivisionError")
    capsentry.find_exception_by_type("TypeError")


def test_sentry_captures_otel_span(capsentry: SentryCapture, tracer) -> None:
    with tracer.start_as_current_span("parent-operation") as parent:
        parent.add_event("Parent's event")

        with tracer.start_as_current_span("child-operation") as child:
            child.add_event("Child's event")

    # Traces are captured without needing an error event.
    capsentry.find_transaction_by_name("parent-operation")
    capsentry.find_span_by_op("child-operation")
