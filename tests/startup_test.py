import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import sentry_sdk

from oauthclientbridge import settings, startup, telemetry
from tests.plugins import sentry


@dataclass(frozen=True)
class FailureCase:
    id: str
    environment: dict[str, str] = field(default_factory=dict)
    error_type: str = "FileNotFoundError"


@dataclass(frozen=True)
class CommandCase:
    id: str
    args: list[str]


@dataclass(frozen=True)
class ConsoleCase:
    id: str
    explicit: bool


@pytest.mark.parametrize(
    "command",
    [
        CommandCase(
            id="cli",
            args=[
                "from oauthclientbridge import cli; cli.main()",
                "serve",
                "--workers",
                "1",
            ],
        ),
        CommandCase(
            id="asgi-factory",
            args=["from oauthclientbridge import asgi; asgi.create_app()"],
        ),
        CommandCase(id="wsgi", args=["import oauthclientbridge.wsgi"]),
    ],
    ids=lambda case: case.id,
)
@pytest.mark.parametrize(
    "failure",
    [
        FailureCase(id="missing-template"),
        FailureCase(
            id="invalid-logging-settings",
            environment={"LOG_LEVEL": "startup-secret-must-not-appear"},
            error_type="ValidationError",
        ),
        FailureCase(
            id="invalid-app-settings",
            environment={
                "BRIDGE_SESSION_COOKIE_SECURE": "startup-secret-must-not-appear"
            },
            error_type="ValidationError",
        ),
        FailureCase(
            id="invalid-settings-json",
            environment={"OAUTH_SCOPES": "startup-secret-must-not-appear"},
            error_type="SettingsError",
        ),
        FailureCase(
            id="invalid-observability-settings",
            environment={"SENTRY_SAMPLE_RATE": "startup-secret-must-not-appear"},
            error_type="ValidationError",
        ),
        FailureCase(
            id="invalid-dictionary-setting",
            environment={
                "BRIDGE_ERROR_LEVELS": '{"startup-secret-must-not-appear": "bad"}'
            },
            error_type="ValidationError",
        ),
    ],
    ids=lambda case: case.id,
)
def test_startup_failure_is_one_safe_json_error(
    tmp_path: Path, command: CommandCase, failure: FailureCase
) -> None:
    missing_template = tmp_path / "missing.html"
    prefixes = ("OAUTH_", "BRIDGE_", "LOG_", "SENTRY_", "TELEMETRY_", "DB_")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(prefixes) and key != "CREDENTIALS_DIRECTORY"
    }
    result = subprocess.run(
        [sys.executable, "-c", *command.args],
        check=False,
        env={
            **environment,
            "OAUTH_CLIENT_ID": "test-client",
            "OAUTH_CLIENT_SECRET": "startup-secret-must-not-appear",
            "OAUTH_AUTHORIZATION_URI": "https://provider.example/authorize",
            "OAUTH_TOKEN_URI": "https://provider.example/token",
            "BRIDGE_SESSION_SECRET": "test-session-secret",
            "BRIDGE_CALLBACK_TEMPLATE_FILE": str(missing_template),
            "LOG_JSON_OUTPUT": "true",
            "SENTRY_ENABLED": "false",
            **failure.environment,
        },
        capture_output=True,
        text=True,
        # Importing the runtime is slower on loaded Nix/CI builders. This is a
        # deadlock guard, not an assertion about startup latency.
        timeout=60,
    )

    assert result.returncode == 1
    assert not result.stdout
    records = [json.loads(line) for line in result.stderr.splitlines()]
    errors = [record for record in records if record["level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["error_type"] == failure.error_type
    if failure.error_type == "FileNotFoundError":
        assert errors[0]["error_filename"] == str(missing_template)
    assert "startup-secret-must-not-appear" not in result.stderr


@pytest.mark.parametrize(
    "case",
    [
        ConsoleCase(id="tty-default", explicit=False),
        ConsoleCase(id="explicit", explicit=True),
    ],
    ids=lambda case: case.id,
)
def test_bootstrap_keeps_console_output(
    case: ConsoleCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("LOG_JSON_OUTPUT", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: not case.explicit)
    if case.explicit:
        monkeypatch.setenv("LOG_JSON_OUTPUT", "false")
    with pytest.raises(SystemExit) as exit, startup.bootstrap():
        raise FileNotFoundError(2, "No such file", "missing.html")

    assert exit.value.code == 1
    output = capsys.readouterr().err
    assert "Startup failed" in output
    assert "missing.html" in output
    assert "FileNotFoundError" in output
    assert not output.startswith("{")
    assert "Traceback (most recent call last)" not in output


def test_sentry_receives_sanitized_settings_failure_before_app_is_created(
    monkeypatch: pytest.MonkeyPatch,
    sentry_transport: sentry.FakeTransport,
) -> None:
    initialize_sentry = telemetry.init_sentry
    client = None

    def with_transport(configured: settings.SentrySettings, runtime: str) -> None:
        nonlocal client
        assert runtime == "starlette"
        try:
            initialize_sentry(configured, "starlette", sentry_transport)
        finally:
            client = sentry_sdk.get_client()

    monkeypatch.setattr(telemetry, "init_sentry", with_transport)
    monkeypatch.setenv("SENTRY_ENABLED", "true")
    monkeypatch.setenv("SENTRY_DSN", "https://key@example.com/1")
    monkeypatch.setenv("BRIDGE_SESSION_COOKIE_SECURE", "validation-input-secret")

    try:
        with pytest.raises(SystemExit) as exit, startup.bootstrap():
            startup.initialize("starlette")

        assert exit.value.code == 1
        assert client is not None
        client.flush()
        events = list(sentry_transport.capture.get_events())
        assert len(events) == 1
        event = events[0]
        assert event["message"] == "Startup failed"
        assert event["contexts"]["structlog"]["error_type"] == "ValidationError"
        assert "validation-input-secret" not in json.dumps(event)
    finally:
        if client is not None:
            client.close()
