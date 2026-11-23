"""Check the installed artifact, not the checkout or deployment environment."""

import io
import json
import subprocess
import sys
from contextlib import redirect_stderr
from importlib import metadata

from prometheus_client import parser
from pydantic import SecretStr

from oauthclientbridge import settings, startup, telemetry


def check_version(expected: dict[str, object]) -> None:
    version = expected["version"]
    revision = expected["revision"] or "unknown"
    dirty = "unknown" if expected["dirty"] is None else str(expected["dirty"]).lower()

    installed = metadata.metadata("oauthclientbridge")

    assert installed["Version"] == version, (installed["Version"], version)
    assert installed["Vcs-Revision"] == revision
    assert installed["Vcs-Dirty"] == dirty
    assert settings.SentrySettings().release == f"oauthclientbridge@{version}"

    configured_telemetry = settings.TelemetrySettings()

    assert configured_telemetry.service_version == version
    assert configured_telemetry.vcs_revision == revision

    output = subprocess.check_output(["oauthclientbridge", "--version"], text=True)

    assert output.strip() == (
        f"oauthclientbridge {version} (revision: {revision}, dirty: {dirty})"
    )

    # Initialize diagnostics with inert endpoints; no server, database, or OAuth
    # request is needed to check what a newly started process reports.
    configured = settings.Settings(
        oauth=settings.OAuthSettings(
            client_id="packaging-check",
            client_secret=SecretStr("packaging-check"),
            authorization_uri="https://example.invalid/authorize",
            token_uri="https://example.invalid/token",
        ),
        otel=configured_telemetry,
    )
    log_output = io.StringIO()
    with (
        redirect_stderr(log_output),
        startup.bootstrap(settings.LogSettings(json_output=True)),
    ):
        startup.initialize(configured)

    events = [json.loads(line) for line in log_output.getvalue().splitlines()]
    event = next(
        event for event in events if event["event"] == "Application initialized"
    )

    assert event["service.version"] == version
    assert event["vcs.revision"] == revision
    assert event["vcs.dirty"] == dirty

    telemetry.set_build_info_metric(configured_telemetry)

    metrics = telemetry.export_metrics(settings.PrometheusSettings()).decode()
    labels = next(
        sample.labels
        for family in parser.text_string_to_metric_families(metrics)
        for sample in family.samples
        if sample.name == "oauth_build_info"
    )

    assert labels["service_version"] == version
    assert labels["vcs_revision"] == revision


if __name__ == "__main__":
    expected = json.loads(sys.argv[1])
    check_version(expected)
