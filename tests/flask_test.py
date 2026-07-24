from http import HTTPStatus

import flask

from oauthclientbridge import bridge
from oauthclientbridge.views import (
    _flask_response,  # pyright: ignore[reportPrivateUsage] # Adapter translation test.
)


def test_flask_adapter_applies_session_and_binary_bridge_response(
    app: flask.Flask,
):
    with app.test_request_context():
        flask.session["stale"] = "value"
        response = _flask_response(
            bridge.BridgeResponse(
                status=HTTPStatus.CREATED,
                headers={"X-Bridge": "yes"},
                body=b"callback body",
                session={"state": "next-state"},
            )
        )

        assert dict(flask.session) == {"state": "next-state"}

    assert response.status_code == HTTPStatus.CREATED
    assert response.data == b"callback body"
    assert response.headers["X-Bridge"] == "yes"
