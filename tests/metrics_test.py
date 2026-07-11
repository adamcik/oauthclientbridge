from datetime import UTC, datetime, timedelta

from oauthclientbridge import create_app, db, stats
from oauthclientbridge.settings import Settings, TelemetrySettings


def test_metrics(client):
    resp = client.get("/metrics")

    assert 200 == resp.status_code
    assert b"auth_server_error_total" in resp.data


def test_metrics_exposes_build_info(settings: Settings):
    settings.otel = TelemetrySettings(
        service_name="oauthclientbridge",
        service_namespace="oauthclientbridge",
        service_version="1.2.3",
        deployment_environment="testing",
        oauth_provider="spotify",
        service_instance_id="oauthclientbridge-spotify-testing",
        vcs_revision="abc1234",
    )

    app = create_app(settings)
    app.secret_key = "test-secret-key"

    with app.app_context():
        db.initialize()
        client = app.test_client()
        resp = client.get("/metrics")

        assert 200 == resp.status_code
        assert (
            b'oauth_build_info{deployment_environment="testing",oauth_provider="spotify",'
            + b'service_instance_id="oauthclientbridge-spotify-testing",'
            + b'service_name="oauthclientbridge",service_namespace="oauthclientbridge",'
            + b'service_version="1.2.3",vcs_revision="abc1234"} 1.0'
        ) in resp.data


def test_metrics_uses_max_aggregation_for_build_info():
    assert stats.BuildInfoGauge._multiprocess_mode == "max"


def test_metrics_exposes_token_state_counts(client):
    _ = db.insert("present-client", b"placeholder")
    _ = db.insert("revoked-client", b"placeholder")
    _ = db.update("revoked-client", None)
    stats.refresh_once(client.application)

    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert b'oauth_token_records{state="present"} 1.0' in resp.data
    assert b'oauth_token_records{state="revoked"} 1.0' in resp.data


def test_metrics_uses_mostrecent_aggregation_for_token_state_counts():
    assert stats.TokenStateGauge._multiprocess_mode == "mostrecent"


def test_metrics_refreshes_token_states_when_run(
    client,
    monkeypatch,
):
    called = False

    def counts() -> dict[str, int]:
        nonlocal called
        called = True
        return {"present": 1, "revoked": 0}

    monkeypatch.setattr(db, "token_state_counts", counts)
    stats.refresh_once(client.application)

    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert called is True


def test_metrics_write_paths_request_background_refresh(client, monkeypatch):
    requested = 0

    def request(app=None) -> None:
        nonlocal requested
        requested += 1

    monkeypatch.setattr(stats, "request_refresh", request)

    db.insert("present-client", b"placeholder")
    db.update("present-client", None)
    db.update("missing-client", None)

    assert requested == 2


def test_metrics_exposes_workaround_counter(
    client, post, access_token, settings: Settings
):
    settings.revoked_grant_workaround_user_agents = r"^Mopidy-Spotify/4\.1\.1\b"

    _ = db.update(access_token.client_id, None)

    _ = post(
        "/token",
        {
            "client_id": access_token.client_id,
            "client_secret": access_token.client_secret,
            "grant_type": "client_credentials",
        },
        headers={"User-Agent": "Mopidy-Spotify/4.1.1 Mopidy/3.4.2 CPython/3.11.2"},
    )

    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert b'oauth_workarounds_total{workaround="revoked_grant"}' in resp.data


def test_metrics_exposes_token_grant_age_histogram_for_successful_token_use(
    cursor,
    monkeypatch,
    post,
    access_token,
):
    created_at = int((datetime.now(UTC) - timedelta(days=200)).timestamp())
    _ = cursor.execute(
        "UPDATE tokens SET created_at = ? WHERE client_id = ?",
        (created_at, access_token.client_id),
    )

    observed: list[float] = []

    def capture(value: float) -> None:
        observed.append(value)

    monkeypatch.setattr(stats.TokenGrantAgeHistogram, "observe", capture)

    resp = post(
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
    cursor,
    monkeypatch,
    post,
    access_token,
):
    _ = cursor.execute(
        "UPDATE tokens SET created_at = NULL WHERE client_id = ?",
        (access_token.client_id,),
    )

    observed: list[float] = []

    def capture(value: float) -> None:
        observed.append(value)

    monkeypatch.setattr(stats.TokenGrantAgeHistogram, "observe", capture)

    resp = post(
        "/token",
        {
            "client_id": access_token.client_id,
            "client_secret": access_token.client_secret,
            "grant_type": "client_credentials",
        },
    )

    assert resp.status == 200
    assert observed == []
