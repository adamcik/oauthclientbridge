from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from oauthclientbridge import oauth, types
from oauthclientbridge.oauth import (
    _httpx as httpx_implementation,  # pyright: ignore[reportPrivateUsage] # Deterministic retry-delay cancellation.
)
from oauthclientbridge.settings import FetchSettings


@pytest.mark.anyio
async def test_httpx_fetcher_retries_retryable_upstream_failure() -> None:
    class OAuthHandler(BaseHTTPRequestHandler):
        requests = 0

        def do_POST(self) -> None:  # noqa: N802 # Required by BaseHTTPRequestHandler.
            type(self).requests += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.requests == 1:
                self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
                self.end_headers()
                return

            body = b'{"access_token":"provider-token","token_type":"Bearer"}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), OAuthHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    fetcher = oauth.create_httpx_fetcher(
        FetchSettings(total_retries=1, backoff_factor=0)
    )

    try:
        result = await fetcher(
            f"http://{host}:{port}/token",
            types.UpstreamGrantType.AUTHORIZATION_CODE,
        )
    finally:
        await fetcher.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join()

    assert result == {"access_token": "provider-token", "token_type": "Bearer"}
    assert OAuthHandler.requests == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "upstream_grant_type",
    [
        types.UpstreamGrantType.AUTHORIZATION_CODE,
        types.UpstreamGrantType.REFRESH_TOKEN,
    ],
)
async def test_httpx_fetcher_rejects_non_success_token_payload(
    upstream_grant_type: types.UpstreamGrantType,
) -> None:
    class OAuthHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 # Required by BaseHTTPRequestHandler.
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b'{"access_token":"provider-token","token_type":"Bearer"}'
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), OAuthHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    fetcher = oauth.create_httpx_fetcher(FetchSettings(total_retries=0))

    try:
        result = await fetcher(f"http://{host}:{port}/token", upstream_grant_type)
    finally:
        await fetcher.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join()

    assert result["error"] == "server_error"
    assert "access_token" not in result


@pytest.mark.anyio
@pytest.mark.parametrize(
    "upstream_grant_type",
    [
        types.UpstreamGrantType.AUTHORIZATION_CODE,
        types.UpstreamGrantType.REFRESH_TOKEN,
    ],
)
async def test_httpx_fetcher_rejects_redirect(
    upstream_grant_type: types.UpstreamGrantType,
) -> None:
    class OAuthHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 # Required by BaseHTTPRequestHandler.
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "https://untrusted.example.com/token")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), OAuthHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    fetcher = oauth.create_httpx_fetcher(FetchSettings(total_retries=0))

    try:
        result = await fetcher(f"http://{host}:{port}/token", upstream_grant_type)
    finally:
        await fetcher.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join()

    assert result["error"] == "server_error"


@pytest.mark.anyio
async def test_httpx_fetcher_preserves_retryable_response_when_retry_delay_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OAuthHandler(BaseHTTPRequestHandler):
        requests = 0

        def do_POST(self) -> None:  # noqa: N802 # Required by BaseHTTPRequestHandler.
            type(self).requests += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Retry-After", "1")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    async def cancel_retry_delay(_: float) -> None:
        raise TimeoutError

    monkeypatch.setattr(httpx_implementation.anyio, "sleep", cancel_retry_delay)
    server = ThreadingHTTPServer(("127.0.0.1", 0), OAuthHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    fetcher = oauth.create_httpx_fetcher(FetchSettings(total_retries=1))

    try:
        result = await fetcher(
            f"http://{host}:{port}/token",
            types.UpstreamGrantType.AUTHORIZATION_CODE,
        )
    finally:
        await fetcher.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join()

    assert result == {
        "error": "temporarily_unavailable",
        "error_description": "Provider is unavailable.",
        "retry_after": 1,
    }
    assert OAuthHandler.requests == 1
