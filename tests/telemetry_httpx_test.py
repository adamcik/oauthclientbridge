import datetime
import ipaddress
import ssl
from collections.abc import Iterator
from pathlib import Path
from threading import Event

import anyio
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from opentelemetry import trace
from pytest_otel_capture import OTelMocker

from oauthclientbridge import telemetry
from oauthclientbridge.settings import PrometheusSettings
from tests.oauth_server import OAuthServer
from tests.prometheus import sample_value


@pytest.fixture
def tls_contexts(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )
    cert_path = tmp_path / "cert.pem"
    cert_path.write_bytes(certificate)
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cadata=certificate.decode())
    return server_context, client_context


@pytest.fixture
def tls_server(
    tls_contexts: tuple[ssl.SSLContext, ssl.SSLContext],
) -> Iterator[OAuthServer]:
    with OAuthServer(tls_context=tls_contexts[0]) as server:
        yield server


@pytest.mark.anyio
async def test_httpx_traces_tls_setup_only_for_new_connections(
    tls_server: OAuthServer,
    tls_contexts: tuple[ssl.SSLContext, ssl.SSLContext],
    otel_mock: OTelMocker,
    instrumented: None,
) -> None:
    before = telemetry.export_metrics(PrometheusSettings())
    for _ in range(2):
        tls_server.expect("/token").respond({"access_token": "response-secret"})

    async with httpx.AsyncClient(verify=tls_contexts[1], trust_env=False) as client:
        for _ in range(2):
            response = await client.post(
                tls_server.url_for("/token"),
                data={"refresh_token": "request-secret"},
                auth=("client-secret", ""),
            )
            assert response.status_code == 200

    spans = otel_mock.get_finished_spans()
    requests = [span for span in spans if span.name == "POST"]
    handshakes = [span for span in spans if span.name == "HTTP connection.start_tls"]
    assert len(requests) == 2
    assert len(handshakes) == 1
    assert handshakes[0].parent_span_id == requests[0].span_id
    assert handshakes[0].status.status_code is trace.StatusCode.UNSET
    assert requests[0].attributes["httpx.connection.setup"] is True
    assert requests[1].attributes["httpx.connection.setup"] is False
    assert len({request.client_address for request in tls_server.requests}) == 1
    for span in spans:
        if span.name.startswith("HTTP "):
            assert not span.attributes
            assert not span.events
            assert span.readable_span.end_time >= span.readable_span.start_time

    after = telemetry.export_metrics(PrometheusSettings())
    assert sample_value(after, "oauth_client_transport_requests_total") == (
        sample_value(before, "oauth_client_transport_requests_total") + 2
    )
    for phase in ("tcp", "tls"):
        for event, count in (("started", 1), ("complete", 1), ("failed", 0)):
            labels = {"phase": phase, "event": event}
            assert sample_value(
                after, "oauth_client_connection_events_total", labels
            ) == (
                sample_value(before, "oauth_client_connection_events_total", labels)
                + count
            )


@pytest.mark.anyio
async def test_httpx_traces_failed_tls_without_exception_details(
    tls_server: OAuthServer,
    otel_mock: OTelMocker,
    instrumented: None,
) -> None:
    before = telemetry.export_metrics(PrometheusSettings())
    # No trusted roots: fail the real handshake without disabling verification.
    async with httpx.AsyncClient(
        verify=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), trust_env=False
    ) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(tls_server.url_for("/token"))

    handshakes = [
        span
        for span in otel_mock.get_finished_spans()
        if span.name == "HTTP connection.start_tls"
    ]
    assert len(handshakes) == 1
    assert handshakes[0].status.status_code is trace.StatusCode.ERROR
    assert handshakes[0].attributes == {"error.type": "ConnectError"}
    assert handshakes[0].status.description is None
    assert not handshakes[0].events

    after = telemetry.export_metrics(PrometheusSettings())
    for event, count in (("started", 1), ("complete", 0), ("failed", 1)):
        labels = {"phase": "tls", "event": event}
        assert sample_value(after, "oauth_client_connection_events_total", labels) == (
            sample_value(before, "oauth_client_connection_events_total", labels) + count
        )


@pytest.mark.anyio
async def test_httpx_connection_metrics_do_not_depend_on_trace_sampling(
    oauth_server: OAuthServer,
    otel_mock: OTelMocker,
    instrumented: None,
) -> None:
    before = telemetry.export_metrics(PrometheusSettings())
    for _ in range(2):
        oauth_server.expect("/token").respond({"access_token": "token"})
    unsampled_parent = trace.NonRecordingSpan(
        trace.SpanContext(
            trace_id=1,
            span_id=1,
            is_remote=True,
            trace_flags=trace.TraceFlags(0),
        )
    )

    with trace.use_span(unsampled_parent):
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(), trust_env=False
        ) as client:
            for _ in range(2):
                response = await client.post(oauth_server.url_for("/token"))
                assert response.status_code == 200

    assert not otel_mock.get_finished_spans()
    assert len({request.client_address for request in oauth_server.requests}) == 1
    after = telemetry.export_metrics(PrometheusSettings())
    assert sample_value(after, "oauth_client_transport_requests_total") == (
        sample_value(before, "oauth_client_transport_requests_total") + 2
    )
    for event, count in (("started", 1), ("complete", 1), ("failed", 0)):
        labels = {"phase": "tcp", "event": event}
        assert sample_value(after, "oauth_client_connection_events_total", labels) == (
            sample_value(before, "oauth_client_connection_events_total", labels) + count
        )


@pytest.mark.anyio
async def test_httpx_preserves_trace_callback_when_resending_request(
    oauth_server: OAuthServer,
    otel_mock: OTelMocker,
    instrumented: None,
) -> None:
    events: list[str] = []

    async def observe(name: str, info: dict[str, object]) -> None:
        events.append(name)

    for _ in range(2):
        oauth_server.expect("/token").respond({"access_token": "token"})
    async with httpx.AsyncClient(
        verify=ssl.create_default_context(), trust_env=False
    ) as client:
        request = client.build_request(
            "POST", oauth_server.url_for("/token"), extensions={"trace": observe}
        )
        for _ in range(2):
            response = await client.send(request)
            assert response.status_code == 200

    assert events.count("http11.send_request_headers.started") == 2
    spans = otel_mock.get_finished_spans()
    requests = [span for span in spans if span.name == "POST"]
    waits = [
        span for span in spans if span.name == "HTTP http11.receive_response_headers"
    ]
    assert len(waits) == 2
    assert {span.parent_span_id for span in waits} == {
        span.span_id for span in requests
    }


@pytest.mark.anyio
async def test_httpx_closes_response_wait_span_when_cancelled(
    oauth_server: OAuthServer,
    otel_mock: OTelMocker,
    instrumented: None,
) -> None:
    waiting = anyio.Event()
    release = Event()
    oauth_server.expect("/token").respond({"access_token": "token"}, hold_until=release)

    async def observe(name: str, info: dict[str, object]) -> None:
        if name == "http11.receive_response_headers.started":
            waiting.set()

    async with httpx.AsyncClient(
        verify=ssl.create_default_context(), trust_env=False
    ) as client:

        async def fetch() -> None:
            await client.post(
                oauth_server.url_for("/token"), extensions={"trace": observe}
            )

        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(fetch)
                await waiting.wait()
                tasks.cancel_scope.cancel()
        finally:
            release.set()

    waits = [
        span
        for span in otel_mock.get_finished_spans()
        if span.name == "HTTP http11.receive_response_headers"
    ]
    assert len(waits) == 1
    assert waits[0].status.status_code is trace.StatusCode.ERROR
    assert not waits[0].events
