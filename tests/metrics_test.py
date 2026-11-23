import sqlite3
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import anyio
import httpx
import pytest
from prometheus_client import parser
from pydantic import SecretStr
from starlette.testclient import TestClient

from oauthclientbridge import crypto, db, oauth, telemetry, types
from oauthclientbridge.asgi import create_app
from oauthclientbridge.asgi_context import TokenStateRefresher
from oauthclientbridge.settings import DatabaseSettings, Settings, TelemetrySettings
from oauthclientbridge.telemetry import _prometheus as stats
from tests.oauth_server import OAuthServer

from .conftest import PostClient, TokenTuple

PRESENT_CLIENT_ID = types.ClientId(uuid.UUID("00000000-0000-0000-0000-000000000001"))
REVOKED_CLIENT_ID = types.ClientId(uuid.UUID("00000000-0000-0000-0000-000000000002"))
MISSING_CLIENT_ID = types.ClientId(uuid.UUID("00000000-0000-0000-0000-000000000003"))
ENCRYPTED_TOKEN = types.EncryptedToken(b"placeholder")


@pytest.fixture
def metrics_database(
    settings: Settings, tmp_path: Path
) -> Generator[sqlite3.Connection]:
    settings.database = DatabaseSettings(database=str(tmp_path / "metrics.sqlite"))
    db.initialize(settings.database)
    connection = sqlite3.connect(settings.database.database, isolation_level=None)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def asgi_client(
    settings: Settings,
    metrics_database: sqlite3.Connection,
    upstream_fetch: oauth.UpstreamFetcher,
) -> Generator[httpx.Client]:
    _ = metrics_database
    app = create_app(
        settings,
        fetch=upstream_fetch,
        initialize_runtime=False,
    )
    client = cast(
        httpx.Client,
        TestClient(app, base_url="https://bridge.example.com", follow_redirects=False),
    )
    try:
        yield client
    finally:
        client.close()


def test_metrics(asgi_client: httpx.Client):
    resp = asgi_client.get("/metrics")

    assert 200 == resp.status_code
    assert b"oauth_server_error_total" in resp.content


def test_metrics_can_be_disabled(settings: Settings, asgi_client: httpx.Client):
    settings.metrics_enabled = False

    response = asgi_client.get("/metrics")

    assert response.status_code == 404


def test_metrics_requires_configured_bearer_token(
    settings: Settings, asgi_client: httpx.Client
):
    settings.metrics_token = SecretStr("metrics-secret")

    unauthorized = asgi_client.get("/metrics")
    authorized = asgi_client.get(
        "/metrics", headers={"Authorization": "Bearer metrics-secret"}
    )

    assert unauthorized.status_code == 401
    assert unauthorized.headers["WWW-Authenticate"] == "Bearer"
    assert authorized.status_code == 200


def test_metrics_reuses_multiprocess_registry(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    settings.prometheus.multiproc_dir = tmp_path
    stats._multiprocess_registries.clear()  # pyright: ignore[reportPrivateUsage] # Direct implementation test.
    collectors: list[object] = []

    def collect(registry: object, path: str) -> None:
        collectors.append(registry)

    monkeypatch.setattr(
        stats.prometheus_client.multiprocess, "MultiProcessCollector", collect
    )

    telemetry.export_metrics(settings.prometheus)
    telemetry.export_metrics(settings.prometheus)

    assert len(collectors) == 1


def test_metrics_exposes_build_info(settings: Settings, asgi_client: httpx.Client):
    settings.otel = TelemetrySettings(
        service_name="oauthclientbridge",
        service_namespace="oauthclientbridge",
        service_version="1.2.3",
        deployment_environment="testing",
        oauth_provider="spotify",
        service_instance_id="oauthclientbridge-spotify-testing",
        vcs_revision="abc1234",
    )

    telemetry.set_build_info_metric(settings.otel)
    resp = asgi_client.get("/metrics")

    assert 200 == resp.status_code
    assert (
        b'oauth_build_info{deployment_environment="testing",oauth_provider="spotify",'
        + b'service_instance_id="oauthclientbridge-spotify-testing",'
        + b'service_name="oauthclientbridge",service_namespace="oauthclientbridge",'
        + b'service_version="1.2.3",vcs_revision="abc1234"} 1.0'
    ) in resp.content


def test_metrics_uses_max_aggregation_for_build_info():
    assert stats.BuildInfoGauge._multiprocess_mode == "max"  # pyright: ignore[reportPrivateUsage] # Direct implementation test.


@pytest.mark.anyio
async def test_metrics_exposes_token_state_counts(
    asgi_client: httpx.Client, settings: Settings
):
    db.insert(PRESENT_CLIENT_ID, ENCRYPTED_TOKEN, settings.database)
    db.insert(REVOKED_CLIENT_ID, ENCRYPTED_TOKEN, settings.database)
    db.update(REVOKED_CLIENT_ID, None, settings.database)
    refresher = TokenStateRefresher(settings.database)
    async with anyio.create_task_group() as group:
        await refresher.start(group)

    resp = asgi_client.get("/metrics")

    assert resp.status_code == 200
    assert b'oauth_token_records{state="present"} 1.0' in resp.content
    assert b'oauth_token_records{state="revoked"} 1.0' in resp.content


def test_metrics_uses_mostrecent_aggregation_for_token_state_counts():
    assert stats.TokenStateGauge._multiprocess_mode == "mostrecent"  # pyright: ignore[reportPrivateUsage] # Direct implementation test.


@pytest.mark.anyio
async def test_metrics_refreshes_token_states_when_requested(
    asgi_client: httpx.Client,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    refreshed = anyio.Event()
    set_counts = telemetry.set_token_state_counts_metric

    def record_counts(counts: dict[str, int]) -> None:
        set_counts(counts)
        refreshed.set()

    monkeypatch.setattr(telemetry, "set_token_state_counts_metric", record_counts)
    refresher = TokenStateRefresher(settings.database)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            await refresher.start(group)
            await refreshed.wait()

            db.insert(PRESENT_CLIENT_ID, ENCRYPTED_TOKEN, settings.database)
            refreshed = anyio.Event()
            await refresher.request()
            await refreshed.wait()
            assert (
                b'oauth_token_records{state="present"} 1.0'
                in asgi_client.get("/metrics").content
            )

            db.update(PRESENT_CLIENT_ID, None, settings.database)
            assert db.update(MISSING_CLIENT_ID, None, settings.database) == 0
            refreshed = anyio.Event()
            await refresher.request()
            await refreshed.wait()

    response = asgi_client.get("/metrics")
    assert b'oauth_token_records{state="present"} 0.0' in response.content
    assert b'oauth_token_records{state="revoked"} 1.0' in response.content


def test_metrics_token_endpoint_requests_background_refresh(
    asgi_client: httpx.Client,
    asgi_post: PostClient,
    refresh_token: TokenTuple,
    settings: Settings,
    upstream_server: OAuthServer,
    monkeypatch: pytest.MonkeyPatch,
):
    requested = 0

    async def request(self: TokenStateRefresher) -> None:
        nonlocal requested
        requested += 1

    monkeypatch.setattr(TokenStateRefresher, "request", request)
    upstream_server.expect("/token").respond({"error": "invalid_grant"})

    response = asgi_post(
        "/token",
        {
            "client_id": refresh_token.client_id,
            "client_secret": refresh_token.client_secret,
            "grant_type": "client_credentials",
        },
    )
    assert response.status == 400
    assert db.lookup(refresh_token.client_id, settings.database).encrypted_token is None
    assert requested == 1


def test_metrics_callback_requests_background_refresh(
    asgi_client: httpx.Client,
    asgi_state: str,
    settings: Settings,
    upstream_server: OAuthServer,
    monkeypatch: pytest.MonkeyPatch,
):
    requested = 0

    async def request(self: TokenStateRefresher) -> None:
        nonlocal requested
        requested += 1

    monkeypatch.setattr(TokenStateRefresher, "request", request)
    upstream_server.expect("/token").respond(
        {"access_token": "new-access", "token_type": "Bearer"}
    )

    response = asgi_client.get(
        "/callback", params={"state": asgi_state, "code": "authorization-code"}
    )

    assert response.status_code == 200
    assert db.token_state_counts(settings.database) == {"present": 1, "revoked": 0}
    assert requested == 1


@pytest.mark.anyio
async def test_asgi_shutdown_stops_background_refresh(
    settings: Settings,
    metrics_database: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
):
    _ = metrics_database
    started = anyio.Event()
    stopped = anyio.Event()
    start_calls = 0

    async def refresh() -> None:
        started.set()
        try:
            await anyio.sleep_forever()
        finally:
            stopped.set()

    async def start(self: TokenStateRefresher, group: anyio.abc.TaskGroup) -> None:
        nonlocal start_calls
        start_calls += 1
        group.start_soon(refresh)

    monkeypatch.setattr(TokenStateRefresher, "start", start)
    app = create_app(settings, initialize_runtime=False)
    assert start_calls == 0
    with anyio.fail_after(5):
        async with app.router.lifespan_context(app):
            await started.wait()
            assert start_calls == 1
        assert stopped.is_set()


def test_metrics_exposes_workaround_counter(
    asgi_client: httpx.Client,
    asgi_post: PostClient,
    access_token: TokenTuple,
    settings: Settings,
):
    settings.revoked_grant_workaround_user_agents = r"^Mopidy-Spotify/4\.1\.1\b"

    _ = db.update(access_token.client_id, None, settings.database)

    _ = asgi_post(
        "/token",
        {
            "client_id": access_token.client_id,
            "client_secret": access_token.client_secret,
            "grant_type": "client_credentials",
        },
        headers={"User-Agent": "Mopidy-Spotify/4.1.1 Mopidy/3.4.2 CPython/3.11.2"},
    )

    resp = asgi_client.get("/metrics")

    assert resp.status_code == 200
    assert b'oauth_workarounds_total{workaround="revoked_grant"}' in resp.content


@dataclass(frozen=True)
class RefreshTokenResponseCase:
    id: str
    response: dict[str, str | int]
    expected_update: str | None
    expected_status: int = 200


@pytest.mark.parametrize(
    "case",
    [
        RefreshTokenResponseCase(
            id="absent",
            response={"access_token": "new-access", "token_type": "Bearer"},
            expected_update="absent",
        ),
        RefreshTokenResponseCase(
            id="unchanged",
            response={
                "access_token": "new-access",
                "token_type": "Bearer",
                "refresh_token": "abc",
            },
            expected_update="unchanged",
        ),
        RefreshTokenResponseCase(
            id="changed",
            response={
                "access_token": "new-access",
                "token_type": "Bearer",
                "refresh_token": "new-refresh",
            },
            expected_update="changed",
        ),
        RefreshTokenResponseCase(
            id="invalid-response-excluded",
            response={"access_token": "new-access", "refresh_token": "new-refresh"},
            expected_update=None,
            expected_status=400,
        ),
        RefreshTokenResponseCase(
            id="oauth-error-excluded",
            response={"error": "invalid_grant"},
            expected_update=None,
            expected_status=400,
        ),
    ],
    ids=lambda case: case.id,
)
def test_metrics_classifies_refresh_token_responses(
    asgi_client: httpx.Client,
    asgi_post: PostClient,
    refresh_token: TokenTuple,
    upstream_server: OAuthServer,
    settings: Settings,
    case: RefreshTokenResponseCase,
):
    upstream_server.expect("/token").respond(case.response)
    before = _refresh_token_response_counts(asgi_client)

    response = asgi_post(
        "/token",
        {
            "client_id": refresh_token.client_id,
            "client_secret": refresh_token.client_secret,
            "grant_type": "client_credentials",
        },
    )

    assert response.status == case.expected_status
    expected = before.copy()
    if case.expected_update is not None:
        expected[case.expected_update] = expected.get(case.expected_update, 0) + 1
        assert "refresh_token" not in response.data
        record = db.lookup(refresh_token.client_id, settings.database)
        assert record.encrypted_token is not None
        stored = crypto.loads(refresh_token.client_secret, record.encrypted_token)
        assert stored["refresh_token"] == case.response.get(
            "refresh_token", refresh_token.value["refresh_token"]
        )
    assert _refresh_token_response_counts(asgi_client) == expected


def test_metrics_excludes_stored_access_token_from_refresh_responses(
    asgi_client: httpx.Client, asgi_post: PostClient, access_token: TokenTuple
):
    before = _refresh_token_response_counts(asgi_client)

    response = asgi_post(
        "/token",
        {
            "client_id": access_token.client_id,
            "client_secret": access_token.client_secret,
            "grant_type": "client_credentials",
        },
    )

    assert response.status == 200
    assert _refresh_token_response_counts(asgi_client) == before


def test_metrics_exposes_token_grant_age_histogram_for_successful_token_use(
    metrics_database: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    asgi_post: PostClient,
    access_token: TokenTuple,
):
    created_at = int((datetime.now(UTC) - timedelta(days=200)).timestamp())
    _ = metrics_database.execute(
        "UPDATE tokens SET created_at = ? WHERE client_id = ?",
        (created_at, str(access_token.client_id)),
    )

    observed: list[float] = []

    def capture(value: float) -> None:
        observed.append(value)

    monkeypatch.setattr(stats.TokenGrantAgeHistogram, "observe", capture)

    resp = asgi_post(
        "/token",
        {
            "client_id": access_token.client_id,
            "client_secret": access_token.client_secret,
            "grant_type": "client_credentials",
        },
    )

    assert resp.status == 200
    assert len(observed) == 1
    assert 200 * 24 * 60 * 60 <= observed[0] < 201 * 24 * 60 * 60


def test_metrics_skips_token_grant_age_histogram_for_unknown_age(
    metrics_database: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    asgi_post: PostClient,
    access_token: TokenTuple,
):
    _ = metrics_database.execute(
        "UPDATE tokens SET created_at = NULL WHERE client_id = ?",
        (str(access_token.client_id),),
    )

    observed: list[float] = []

    def capture(value: float) -> None:
        observed.append(value)

    monkeypatch.setattr(stats.TokenGrantAgeHistogram, "observe", capture)

    resp = asgi_post(
        "/token",
        {
            "client_id": access_token.client_id,
            "client_secret": access_token.client_secret,
            "grant_type": "client_credentials",
        },
    )

    assert resp.status == 200
    assert observed == []


def _refresh_token_response_counts(client: httpx.Client) -> dict[str, float]:
    response = client.get("/metrics")
    assert response.status_code == 200
    counts: dict[str, float] = {}
    for family in parser.text_string_to_metric_families(response.text):
        for sample in family.samples:
            if sample.name == "oauth_refresh_token_responses_total":
                assert set(sample.labels) == {"token_update"}
                outcome = sample.labels["token_update"]
                assert outcome in {"absent", "unchanged", "changed"}
                counts[outcome] = sample.value
    return counts
