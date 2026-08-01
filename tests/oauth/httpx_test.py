from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import anyio
import pytest

from oauthclientbridge import oauth, types
from oauthclientbridge.oauth import (
    _httpx as httpx_implementation,  # pyright: ignore[reportPrivateUsage] # Deterministic retry-delay cancellation.
)
from oauthclientbridge.settings import FetchSettings


@pytest.mark.anyio
async def test_httpx_fetcher_retries_retryable_upstream_failure() -> None:
    class OAuthHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
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
    client = oauth.create_httpx_upstream_client(
        FetchSettings(total_retries=1, backoff_factor=0)
    )

    try:
        result = await client.fetch(
            f"http://{host}:{port}/token",
            types.UpstreamGrantType.AUTHORIZATION_CODE,
        )
    finally:
        await client.aclose()
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
        protocol_version = "HTTP/1.1"

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
    client = oauth.create_httpx_upstream_client(FetchSettings(total_retries=0))

    try:
        result = await client.fetch(f"http://{host}:{port}/token", upstream_grant_type)
    finally:
        await client.aclose()
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
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 # Required by BaseHTTPRequestHandler.
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "https://untrusted.example.com/token")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), OAuthHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    client = oauth.create_httpx_upstream_client(FetchSettings(total_retries=0))

    try:
        result = await client.fetch(f"http://{host}:{port}/token", upstream_grant_type)
    finally:
        await client.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join()

    assert result["error"] == "server_error"


@pytest.mark.anyio
async def test_httpx_fetcher_preserves_retryable_response_when_retry_delay_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OAuthHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        requests = 0

        def do_POST(self) -> None:  # noqa: N802 # Required by BaseHTTPRequestHandler.
            type(self).requests += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", "0")
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
    client = oauth.create_httpx_upstream_client(FetchSettings(total_retries=1))

    try:
        result = await client.fetch(
            f"http://{host}:{port}/token",
            types.UpstreamGrantType.AUTHORIZATION_CODE,
        )
    finally:
        await client.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join()

    assert result == {
        "error": "temporarily_unavailable",
        "error_description": "Provider is unavailable.",
        "retry_after": 1,
    }
    assert OAuthHandler.requests == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "upstream_grant_type",
    [
        types.UpstreamGrantType.AUTHORIZATION_CODE,
        types.UpstreamGrantType.REFRESH_TOKEN,
    ],
)
async def test_httpx_fetcher_reuses_http11_connection(
    upstream_grant_type: types.UpstreamGrantType,
) -> None:
    class OAuthHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        client_addresses: set[tuple[str, int]] = set()

        def do_POST(self) -> None:  # noqa: N802 # Required by BaseHTTPRequestHandler.
            type(self).client_addresses.add(self.client_address)
            self.rfile.read(int(self.headers["Content-Length"]))
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
    client = oauth.create_httpx_upstream_client(FetchSettings(total_retries=0))

    try:
        for _ in range(2):
            result = await client.fetch(
                f"http://{host}:{port}/token", upstream_grant_type
            )
            assert result == {"access_token": "provider-token", "token_type": "Bearer"}
    finally:
        await client.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join()

    assert len(OAuthHandler.client_addresses) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "upstream_grant_type",
    [
        types.UpstreamGrantType.AUTHORIZATION_CODE,
        types.UpstreamGrantType.REFRESH_TOKEN,
    ],
)
async def test_httpx_fetcher_cancels_inflight_request_and_closes_within_deadline(
    upstream_grant_type: types.UpstreamGrantType,
) -> None:
    request_started = Event()
    release_request = Event()

    class OAuthHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 # Required by BaseHTTPRequestHandler.
            self.rfile.read(int(self.headers["Content-Length"]))
            request_started.set()
            assert release_request.wait(timeout=1)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), OAuthHandler)
    server.daemon_threads = True
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    client = oauth.create_httpx_upstream_client(FetchSettings(total_retries=0))
    cancelled = False

    async def fetch() -> None:
        nonlocal cancelled
        try:
            await client.fetch(f"http://{host}:{port}/token", upstream_grant_type)
        except anyio.get_cancelled_exc_class():
            cancelled = True

    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(fetch)
            assert await anyio.to_thread.run_sync(request_started.wait, 1)
            task_group.cancel_scope.cancel()

        with anyio.fail_after(1):
            await client.aclose()
    finally:
        release_request.set()
        server.shutdown()
        server.server_close()
        server_thread.join()

    assert cancelled is True
