from http import HTTPStatus

import httpx
import pytest
from flask import jsonify, request
from starlette.requests import Request
from starlette.responses import JSONResponse

from oauthclientbridge import create_app
from oauthclientbridge.asgi import create_app as create_asgi_app
from oauthclientbridge.settings import Settings

FORWARDED_HEADERS = {
    "X-Forwarded-For": "198.51.100.42",
    "X-Forwarded-Host": "bridge.example.com",
    "X-Forwarded-Port": "8443",
    "X-Forwarded-Proto": "https",
}


def test_flask_adapter_uses_caddy_forwarded_headers(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/request-info")
    def request_info():
        return jsonify(
            client=request.remote_addr,
            host=request.host,
            scheme=request.scheme,
        )

    response = app.test_client().get("/request-info", headers=FORWARDED_HEADERS)

    assert response.status_code == HTTPStatus.OK
    assert response.json == {
        "client": "198.51.100.42",
        "host": "bridge.example.com:8443",
        "scheme": "https",
    }


@pytest.mark.anyio
async def test_starlette_adapter_uses_caddy_forwarded_headers(
    settings: Settings,
) -> None:
    app = create_asgi_app(settings, initialize_runtime=False)

    async def request_info(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "client": request.client.host if request.client is not None else None,
                "host": request.url.netloc,
                "scheme": request.url.scheme,
            }
        )

    app.add_route("/request-info", request_info)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost"
    ) as client:
        response = await client.get("/request-info", headers=FORWARDED_HEADERS)

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "client": "198.51.100.42",
        "host": "bridge.example.com:8443",
        "scheme": "https",
    }
