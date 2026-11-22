from http import HTTPStatus

from starlette.requests import Request

from oauthclientbridge import bridge
from oauthclientbridge.routes import (
    _response,  # pyright: ignore[reportPrivateUsage] # Direct adapter translation test.
)


def test_adapter_preserves_binary_body_status_headers_and_session() -> None:
    request = Request(
        {
            "type": "http",
            "session": {"oauthclientbridge": {"state": "stale"}},
        }
    )
    response = _response(
        request,
        bridge.BridgeResponse(
            status=HTTPStatus.CREATED,
            headers={"X-Bridge": "yes"},
            body=b"callback body",
            session={"state": "next-state"},
        ),
    )

    assert request.session == {"oauthclientbridge": {"state": "next-state"}}
    assert response.status_code == HTTPStatus.CREATED
    assert response.body == b"callback body"
    assert response.headers["X-Bridge"] == "yes"
