import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import SecretStr

from oauthclientbridge import settings


@pytest.fixture(autouse=True)
def oauth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("OAUTH_AUTHORIZATION_URI", "https://provider.example/authorize")
    monkeypatch.setenv("OAUTH_TOKEN_URI", "https://provider.example/token")
    for key in (
        "CREDENTIALS_DIRECTORY",
        "OAUTH_CLIENT_SECRET",
        "BRIDGE_SESSION_SECRET",
        "BRIDGE_METRICS_TOKEN",
        "SENTRY_DSN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_reads_credentials_without_exporting_them_to_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = {
        "OAUTH_CLIENT_SECRET": "provider-secret",
        "BRIDGE_SESSION_SECRET": "session-secret",
        "BRIDGE_METRICS_TOKEN": "metrics-secret",
        "SENTRY_DSN": "https://public@example.com/1",
    }
    for key, value in credentials.items():
        (tmp_path / key).write_text(value + "\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))

    configured = settings.Settings()

    assert configured.oauth.client_secret.get_secret_value() == "provider-secret"
    assert configured.session_secret == SecretStr("session-secret")
    assert configured.metrics_token == SecretStr("metrics-secret")
    assert configured.sentry.dsn == SecretStr("https://public@example.com/1")
    assert all(key not in os.environ for key in credentials)


@dataclass(frozen=True, kw_only=True)
class PrecedenceCase:
    environment: str | None
    explicit: SecretStr | None
    expected: str


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            PrecedenceCase(environment=None, explicit=None, expected="file-secret"),
            id="credential-file",
        ),
        pytest.param(
            PrecedenceCase(
                environment="env-secret", explicit=None, expected="env-secret"
            ),
            id="environment-over-file",
        ),
        pytest.param(
            PrecedenceCase(
                environment="env-secret",
                explicit=SecretStr("explicit-secret"),
                expected="explicit-secret",
            ),
            id="explicit-over-environment",
        ),
    ],
)
def test_credentials_follow_normal_settings_precedence(
    case: PrecedenceCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "OAUTH_CLIENT_SECRET").write_text("file-secret")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    if case.environment is not None:
        monkeypatch.setenv("OAUTH_CLIENT_SECRET", case.environment)

    configured = (
        settings.OAuthSettings(client_secret=case.explicit)
        if case.explicit is not None
        else settings.OAuthSettings()
    )

    assert configured.client_secret.get_secret_value() == case.expected


def test_credentials_directory_is_resolved_at_settings_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for value in ("first-secret", "second-secret"):
        directory = tmp_path / value
        directory.mkdir()
        (directory / "OAUTH_CLIENT_SECRET").write_text(value)
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory))

        assert settings.OAuthSettings().client_secret.get_secret_value() == value


def test_explicit_secret_directory_keeps_its_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    (inherited / "OAUTH_CLIENT_SECRET").write_text("inherited-secret")
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    (explicit / "OAUTH_CLIENT_SECRET").write_text("explicit-secret")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(inherited))

    configured = settings.OAuthSettings(_secrets_dir=explicit)

    assert configured.client_secret.get_secret_value() == "explicit-secret"


def test_environment_only_configuration_remains_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", "environment-secret")
    monkeypatch.setenv("BRIDGE_SESSION_SECRET", "environment-session")

    configured = settings.Settings()

    assert configured.oauth.client_secret.get_secret_value() == "environment-secret"
    assert configured.session_secret == SecretStr("environment-session")
