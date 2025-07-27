import importlib
import sys
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from oauthclientbridge import sentry
from oauthclientbridge.settings import SentrySettings


@pytest.fixture
def sentry_settings() -> SentrySettings:
    """A pytest fixture that provides SentrySettings."""
    return SentrySettings(
        enabled=True,
        dsn=SecretStr("http://test:test@localhost/1"),
        sample_rate=0.5,
        traces_sample_rate=0.2,
    )


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
