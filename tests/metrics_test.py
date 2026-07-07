from oauthclientbridge import create_app, db
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
