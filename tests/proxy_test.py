import flask

from oauthclientbridge import create_app
from oauthclientbridge.settings import Settings


def test_proxy_fix_uses_forwarded_proto_host_port(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"num_proxies": 1}))

    @app.get("/proxy-debug")
    def proxy_debug() -> dict[str, str | None]:
        return {
            "remote_addr": flask.request.remote_addr,
            "host": flask.request.host,
            "scheme": flask.request.scheme,
            "url_root": flask.request.url_root,
        }

    with app.test_client() as client:
        response = client.get(
            "/proxy-debug",
            headers={
                "X-Forwarded-For": "203.0.113.77",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "auth.mopidy.com",
                "X-Forwarded-Port": "8443",
            },
        )

    assert response.status_code == 200

    payload = response.get_json()
    assert payload == {
        "remote_addr": "203.0.113.77",
        "host": "auth.mopidy.com:8443",
        "scheme": "https",
        "url_root": "https://auth.mopidy.com:8443/",
    }
