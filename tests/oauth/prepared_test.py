import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from oauthclientbridge import oauth, telemetry, transport, types
from oauthclientbridge.asgi import create_app
from oauthclientbridge.settings import (
    FetchSettings,
    FetchTransport,
    PrometheusSettings,
    Settings,
)
from tests.http2_peer import HTTP2Peer, Response
from tests.plugins.otel import OTelMocker
from tests.prometheus import sample_value


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_TOKEN = b'{"access_token":"local-only","token_type":"Bearer"}'


@dataclass(frozen=True)
class ProtocolCase:
    id: str
    alpn: str | None
    versions: tuple[transport.HTTPVersion, ...]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        ProtocolCase(id="h2", alpn="h2", versions=("HTTP/2",)),
        ProtocolCase(id="h1", alpn="http/1.1", versions=("HTTP/2", "HTTP/1.1")),
        ProtocolCase(id="no-alpn", alpn=None, versions=("HTTP/2", "HTTP/1.1")),
    ],
    ids=lambda case: case.id,
)
async def test_prepared_fetch_uses_startup_socket_and_preserves_httpx_tracing(
    tmp_path: Path,
    settings: Settings,
    case: ProtocolCase,
    otel_mock: OTelMocker,
    instrumented: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A custom transport must not be bypassed by environment proxy routing.
    monkeypatch.setenv("HTTPS_PROXY", "http://[::1]:1")
    async with HTTP2Peer(tmp_path, alpn=case.alpn) as peer:
        peer.responses = [Response(status=200, body=_TOKEN) for _ in range(2)]
        settings.oauth.token_uri = peer.origin + "/token"
        settings.oauth.refresh_uri = peer.origin + "/refresh"
        client = oauth.create_httpx_upstream_client(
            FetchSettings(
                transport=FetchTransport.PREPARED, prepared_versions=case.versions
            ),
            origin=settings.oauth.prepared_origin(),
            ssl_context=peer.trusted,
        )
        try:
            await client.start()
            await client.start()
            await peer.until(lambda: bool(peer.accepted) and peer.accepted[0].handshake)
            assert not peer.operations and peer.accepted[0].plaintext_bytes == 0
            for uri, grant_type in (
                (settings.oauth.token_uri, types.UpstreamGrantType.AUTHORIZATION_CODE),
                (settings.oauth.refresh_uri, types.UpstreamGrantType.REFRESH_TOKEN),
            ):
                result = await client.fetch(uri, grant_type, grant_type=grant_type)
                assert result == {"access_token": "local-only", "token_type": "Bearer"}
            assert [operation.connection_id for operation in peer.operations] == [1, 1]
            assert len(peer.accepted) == 1
            assert all(
                "traceparent" in operation.headers for operation in peer.operations
            )
        finally:
            await client.aclose()
        await peer.until(lambda: peer.accepted[0].done.is_set())

    spans = otel_mock.get_finished_spans()
    requests = [span for span in spans if span.name == "POST"]
    assert len(requests) == 2
    assert all(span.attributes["httpx.connection.setup"] is False for span in requests)
    assert len([span for span in spans if span.name.startswith("OAUTH ")]) == 2
    phase = "http2" if case.alpn == "h2" else "http11"
    waits = [
        span for span in spans if span.name == f"HTTP {phase}.receive_response_headers"
    ]
    assert {span.parent_span_id for span in waits} == {
        span.span_id for span in requests
    }


@dataclass(frozen=True)
class FailureCase:
    id: str
    path: str
    alpn: str


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        FailureCase(id="h2-response-loss", path="/lost", alpn="h2"),
        FailureCase(id="h2-stream-reset", path="/reset", alpn="h2"),
        FailureCase(id="h2-read-timeout", path="/hold", alpn="h2"),
        FailureCase(id="h1-response-loss", path="/lost", alpn="http/1.1"),
        FailureCase(id="h1-read-timeout", path="/hold", alpn="http/1.1"),
    ],
    ids=lambda case: case.id,
)
async def test_prepared_fetch_never_replays_transport_failures(
    tmp_path: Path,
    settings: Settings,
    case: FailureCase,
) -> None:
    async with HTTP2Peer(tmp_path, alpn=case.alpn) as peer:
        settings.oauth.token_uri = peer.origin + case.path
        client = oauth.create_httpx_upstream_client(
            FetchSettings(
                transport=FetchTransport.PREPARED,
                total_attempts=3,
                timeout=0.2,
                backoff_factor=0,
                prepared_versions=("HTTP/2", "HTTP/1.1"),
            ),
            origin=settings.oauth.prepared_origin(),
            ssl_context=peer.trusted,
        )
        try:
            await client.start()
            result = await client.fetch(
                settings.oauth.token_uri,
                types.UpstreamGrantType.AUTHORIZATION_CODE,
                code="local-only",
            )
            assert result["error"] in {"server_error", "temporarily_unavailable"}
            assert len(peer.operations) == 1
            assert peer.operations[0].body == b"code=local-only"
        finally:
            await client.aclose()


@pytest.mark.anyio
async def test_prepared_http_response_retry_does_not_rotate_client_or_connection(
    tmp_path: Path,
    settings: Settings,
) -> None:
    before = telemetry.export_metrics(PrometheusSettings())
    async with HTTP2Peer(tmp_path) as peer:
        peer.responses = [
            Response(status=503, body=b"{}"),
            Response(status=200, body=_TOKEN),
        ]
        settings.oauth.token_uri = peer.origin + "/token"
        client = oauth.create_httpx_upstream_client(
            FetchSettings(
                transport=FetchTransport.PREPARED, total_attempts=2, backoff_factor=0
            ),
            origin=settings.oauth.prepared_origin(),
            ssl_context=peer.trusted,
        )
        try:
            await client.start()
            assert await client.fetch(
                settings.oauth.token_uri, types.UpstreamGrantType.REFRESH_TOKEN
            ) == {"access_token": "local-only", "token_type": "Bearer"}
            assert [operation.connection_id for operation in peer.operations] == [1, 1]
            assert len(peer.accepted) == 1
        finally:
            await client.aclose()
    after = telemetry.export_metrics(PrometheusSettings())
    labels = {"endpoint": "refresh_token", "error": "http_503"}
    assert sample_value(
        after, "oauth_client_generation_resets_total", labels
    ) == sample_value(before, "oauth_client_generation_resets_total", labels)


@pytest.mark.anyio
async def test_prepared_admission_is_bounded_and_does_not_connect_on_fetch(
    tmp_path: Path,
    settings: Settings,
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        settings.oauth.token_uri = peer.origin + "/token"
        client = oauth.create_httpx_upstream_client(
            FetchSettings(transport=FetchTransport.PREPARED, prepared_max_streams=1),
            origin=settings.oauth.prepared_origin(),
            ssl_context=peer.trusted,
        )
        held: asyncio.Task[object] | None = None
        try:
            result = await client.fetch(
                settings.oauth.token_uri, types.UpstreamGrantType.REFRESH_TOKEN
            )
            assert result["error"] == "temporarily_unavailable"
            assert not peer.accepted
            await client.start()
            held = asyncio.create_task(
                client.fetch(
                    peer.origin + "/hold", types.UpstreamGrantType.REFRESH_TOKEN
                )
            )
            await peer.until(lambda: len(peer.operations) == 1)
            saturated = await client.fetch(
                settings.oauth.token_uri, types.UpstreamGrantType.REFRESH_TOKEN
            )
            mismatched = await client.fetch(
                "https://unconfigured.invalid/token",
                types.UpstreamGrantType.REFRESH_TOKEN,
            )
            assert (
                saturated["error"] == mismatched["error"] == "temporarily_unavailable"
            )
            assert len(peer.operations) == len(peer.accepted) == 1
        finally:
            if held is not None:
                held.cancel()
                await asyncio.gather(held, return_exceptions=True)
            await client.aclose()


@pytest.mark.anyio
async def test_prepared_shutdown_drains_cancelled_fetches_before_closing(
    tmp_path: Path,
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        client = oauth.create_httpx_upstream_client(
            FetchSettings(transport=FetchTransport.PREPARED),
            origin=peer.origin,
            ssl_context=peer.trusted,
        )
        held: asyncio.Task[object] | None = None
        try:
            await client.start()
            held = asyncio.create_task(
                client.fetch(
                    peer.origin + "/hold", types.UpstreamGrantType.AUTHORIZATION_CODE
                )
            )
            await peer.until(lambda: len(peer.operations) == 1)
            held.cancel()
            async with asyncio.timeout(2):
                await asyncio.gather(client.aclose(), client.aclose())
                with pytest.raises(asyncio.CancelledError):
                    await held
            await peer.until(lambda: peer.accepted[0].done.is_set())
            assert len(peer.operations) == 1
            with pytest.raises(RuntimeError, match="shutting down"):
                await client.fetch(
                    peer.origin + "/token", types.UpstreamGrantType.REFRESH_TOKEN
                )
        finally:
            if held is not None:
                held.cancel()
                await asyncio.gather(held, return_exceptions=True)
            await client.aclose()


@pytest.mark.anyio
async def test_asgi_prepares_before_serving_and_closes_on_exit(
    tmp_path: Path,
    settings: Settings,
    initialized_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca_path))
        # Nix's OpenSSL gives this platform override precedence over SSL_CERT_FILE.
        monkeypatch.setenv("NIX_SSL_CERT_FILE", str(peer.ca_path))
        settings.oauth.token_uri = peer.origin + "/token"
        settings.fetch = FetchSettings(transport=FetchTransport.PREPARED)
        app = create_app(settings, initialize_runtime=False)
        assert not peer.accepted
        async with app.router.lifespan_context(app):
            await peer.until(lambda: bool(peer.accepted) and peer.accepted[0].handshake)
            assert not peer.operations and peer.accepted[0].plaintext_bytes == 0
        await peer.until(lambda: peer.accepted[0].done.is_set())


@pytest.mark.anyio
async def test_asgi_refuses_unverified_preparation_and_cleans_up(
    tmp_path: Path,
    settings: Settings,
    initialized_database: None,
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        settings.oauth.token_uri = peer.origin + "/token"
        settings.fetch = FetchSettings(transport=FetchTransport.PREPARED)
        app = create_app(settings, initialize_runtime=False)
        with pytest.raises(Exception, match="CERTIFICATE_VERIFY_FAILED"):
            async with app.router.lifespan_context(app):
                pytest.fail("must not serve without verified preparation")
        await peer.until(lambda: bool(peer.accepted) and peer.accepted[0].done.is_set())
        assert not peer.operations
