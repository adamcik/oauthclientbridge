import asyncio
import ssl
from collections.abc import AsyncIterator, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path

import h2.events
import h2.settings
import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    HistogramDataPoint,
    InMemoryMetricReader,
    NumberDataPoint,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from oauthclientbridge import transport
from tests.http2_peer import HTTP2Peer


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Capture:
    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        self.provider = MeterProvider(metric_readers=[self.reader])

    def points(
        self, name: str, attributes: Mapping[str, str | int | bool] | None = None
    ) -> list[NumberDataPoint | HistogramDataPoint]:
        data = self.reader.get_metrics_data()
        assert data is not None
        return [
            point
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == "oauth.transport." + name
            for point in metric.data.data_points
            if isinstance(point, (NumberDataPoint, HistogramDataPoint))
            and all(
                (point.attributes or {}).get(key) == value
                for key, value in (attributes or {}).items()
            )
        ]

    def value(self, name: str, **attributes: str | int | bool) -> int | float:
        points = self.points(name, attributes)
        assert points, f"metric not recorded: {name} {attributes}"
        return sum(
            point.value for point in points if isinstance(point, NumberDataPoint)
        )

    def count(self, name: str, **attributes: str | int | bool) -> int:
        return sum(
            point.count
            for point in self.points(name, attributes)
            if isinstance(point, HistogramDataPoint)
        )

    def total(self, name: str, **attributes: str | int | bool) -> int | float:
        return sum(
            point.sum
            for point in self.points(name, attributes)
            if isinstance(point, HistogramDataPoint)
        )


@pytest.fixture
def capture() -> Generator[_Capture]:
    result = _Capture()
    try:
        yield result
    finally:
        result.provider.shutdown()


@pytest.mark.anyio
async def test_stream_metrics_distinguish_peer_credit_wait_from_allocated_streams(
    tmp_path: Path, capture: _Capture
) -> None:
    admitted_two = asyncio.Event()

    def observe(event: transport.TransportEvent) -> None:
        if event.kind == "acquired" and event.state.active_leases == 2:
            admitted_two.set()

    async with HTTP2Peer(tmp_path, max_streams=1) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin,
            ssl_context=peer.trusted,
            meter_provider=capture.provider,
            observer=observe,
            max_streams=2,
        )
        async with (
            httpx.AsyncClient(transport=prepared) as client,
            client.stream("POST", peer.origin + "/hold", content=b"first") as first,
        ):
            assert len(peer.headers_received) == 1
            assert capture.value("streams.active") == 1
            assert capture.value("streams.pending") == 0
            second = asyncio.create_task(
                client.post(peer.origin + "/token", content=b"queued")
            )
            async with asyncio.timeout(5):
                await admitted_two.wait()
            assert prepared.snapshot().active_leases == 2
            assert len(peer.headers_received) == 1
            assert capture.value("streams.active") == 1
            assert capture.value("streams.pending") == 1
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert capture.value("streams.pending") == 0
            assert capture.value("streams.started") == 1
            await peer.release(1, first.extensions["stream_id"])
            assert await first.aread() == b"1"
            await first.aclose()
        assert capture.value("streams.active") == 0
        assert capture.value("streams.closed", outcome="complete") == 1
        assert capture.count("stream.wait.duration", outcome="cancelled") == 1


@pytest.mark.anyio
async def test_connection_metrics_include_unpublished_tcp_candidates_and_cleanup(
    tmp_path: Path, capture: _Capture
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted, meter_provider=capture.provider
        )
        async with httpx.AsyncClient(transport=prepared):
            peer.tls_gate.clear()
            builder = asyncio.create_task(prepared.cycle())
            try:
                await peer.until(lambda: len(peer.accepted) == 2)
                await peer.client_hello(2)
                assert capture.value("connections.open") == 2
                assert sum(connection.handshake for connection in peer.accepted) == 1
                assert capture.value("connections", state="preparing") == 1
                builder.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await builder
                assert capture.value("connections.open") == 1
                assert capture.count("connection.duration") == 1
            finally:
                builder.cancel()
                await asyncio.gather(builder, return_exceptions=True)
                peer.tls_gate.set()
        await peer.until(
            lambda: all(connection.done.is_set() for connection in peer.accepted)
        )
        assert capture.value("connections.open") == 0
        assert capture.value("connections.opened") == len(peer.accepted)
        assert capture.value("connections.closed") == len(peer.accepted)
        assert capture.count("connection.duration") == len(peer.accepted)
        assert not peer.headers_received


@pytest.mark.anyio
async def test_body_and_connection_bytes_match_the_peer_and_preserve_caller_tracing(
    tmp_path: Path, capture: _Capture
) -> None:
    seen: list[tuple[str, object]] = []

    async def previous_trace(name: str, info: dict[str, object]) -> None:
        seen.append((name, info.get("stream_id")))

    payload = b"local-payload-" * 40

    async def chunks() -> AsyncIterator[bytes]:
        yield payload[:19]
        yield payload[19:]

    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted, meter_provider=capture.provider
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            request = client.build_request(
                "POST",
                peer.origin + "/private-path",
                content=chunks(),
                headers={"authorization": "local-sensitive-header"},
                extensions={"trace": previous_trace},
            )
            response = await client.send(request)
            assert response.text == "1" and peer.operations[0].body == payload
            assert request.extensions["trace"] is previous_trace
            stream_id = response.extensions["stream_id"]
            assert seen.count(("http2.send_request_headers.started", stream_id)) == 1
            assert [name for name, _ in seen].count(
                "http2.response_closed.complete"
            ) == 1
            sent = capture.value("connection.io", direction="sent")
            await peer.until(lambda: peer.accepted[0].plaintext_bytes == sent)
        await peer.until(lambda: peer.accepted[0].done.is_set())
        assert capture.total("stream.request.body.size", outcome="complete") == len(
            peer.operations[0].body
        )
        assert capture.total("stream.response.body.size", outcome="complete") == len(
            response.content
        )
        assert (
            capture.value("connection.io", direction="sent")
            == peer.accepted[0].plaintext_bytes
        )
        assert (
            capture.value("connection.io", direction="received")
            == peer.accepted[0].plaintext_sent
        )
        assert capture.total("connection.streams") == len(peer.headers_received)
        assert capture.count("stream.duration", outcome="complete") == 1
        assert capture.value("connections.open") == 0
        data = capture.reader.get_metrics_data()
        assert data is not None
        allowed = {
            "server.address",
            "server.port",
            "phase",
            "event",
            "reason",
            "outcome",
            "direction",
            "state",
            "forced",
            "network.protocol.version",
        }
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        assert set(point.attributes or {}) <= allowed
                        assert "private-path" not in str(point.attributes)
                        assert "local-sensitive-header" not in str(point.attributes)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.changed = asyncio.Event()

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds
        self.changed.set()

    async def sleep_until(self, deadline: float) -> None:
        while self.value < deadline:
            await self.changed.wait()
            self.changed.clear()


@pytest.mark.anyio
async def test_socket_close_ends_stream_metrics_but_not_the_callers_response_lease(
    tmp_path: Path, capture: _Capture
) -> None:
    replacement, closed = asyncio.Event(), asyncio.Event()
    clock = _Clock()

    def observe(event: transport.TransportEvent) -> None:
        if event.kind == "published" and event.connection_id == 2:
            replacement.set()
        elif event.kind == "forced_closed" and event.connection_id == 1:
            closed.set()

    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin,
            ssl_context=peer.trusted,
            meter_provider=capture.provider,
            observer=observe,
            clock=clock,
            drain_timeout=3,
        )
        async with (
            httpx.AsyncClient(transport=prepared) as client,
            client.stream("POST", peer.origin + "/hold", content=b"held") as response,
        ):
            assert capture.value("streams.active") == 1
            prepared.discard(1)
            async with asyncio.timeout(5):
                await replacement.wait()
            clock.advance(3)
            async with asyncio.timeout(5):
                await closed.wait()
            assert capture.value("connections.open") == 1
            assert capture.value("streams.active") == 0
            assert capture.value("leases") == 1
            assert capture.count("stream.duration") == 0
            with pytest.raises(httpx.RequestError):
                await response.aread()
            await response.aclose()
            assert capture.value("leases") == 0
            assert capture.value("streams.active") == 0
            assert capture.value("streams.closed", outcome="failed") == 1
            assert capture.count("stream.duration", outcome="failed") == 1
        assert capture.value("connections.open") == 0
        assert len(peer.operations) == 1


@pytest.mark.anyio
async def test_interrupted_httpcore_release_stays_active_until_socket_cleanup(
    tmp_path: Path, capture: _Capture
) -> None:
    async def interrupt_release(name: str, info: dict[str, object]) -> None:
        if name == "http2.response_closed.started":
            raise RuntimeError("caller interrupted HTTPcore release")

    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted, meter_provider=capture.provider
        )
        async with (
            httpx.AsyncClient(transport=prepared) as client,
            client.stream("POST", peer.origin + "/hold", content=b"survivor") as first,
        ):
            with pytest.raises(RuntimeError, match="interrupted HTTPcore release"):
                await client.post(
                    peer.origin + "/token",
                    content=b"other",
                    extensions={"trace": interrupt_release},
                )
            assert prepared.snapshot().active_leases == 1
            assert capture.value("streams.active") == len(peer.headers_received) == 2
            assert capture.count("stream.duration", outcome="failed") == 0
            await peer.release(1, first.extensions["stream_id"])
            assert await first.aread() == b"1"
            await first.aclose()
            await peer.until(lambda: peer.accepted[0].done.is_set())
            assert capture.value("streams.active") == 0
            assert capture.value("streams.closed", outcome="failed") == 1
            assert capture.count("stream.duration", outcome="failed") == 1
        assert capture.value("connections.open") == 0


@dataclass(frozen=True)
class PreparationFailureCase:
    id: str
    alpn: str
    trust_peer: bool
    error: type[httpx.TransportError]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        PreparationFailureCase(
            id="failed-tls", alpn="h2", trust_peer=False, error=httpx.ConnectError
        ),
        PreparationFailureCase(
            id="failed-alpn",
            alpn="http/1.1",
            trust_peer=True,
            error=httpx.UnsupportedProtocol,
        ),
    ],
    ids=lambda case: case.id,
)
async def test_failed_preparation_balances_tcp_metrics_without_inventing_streams(
    tmp_path: Path, capture: _Capture, case: PreparationFailureCase
) -> None:
    async with HTTP2Peer(tmp_path, alpn=case.alpn) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin,
            meter_provider=capture.provider,
            ssl_context=peer.trusted
            if case.trust_peer
            else ssl.create_default_context(),
        )
        try:
            with pytest.raises(case.error):
                await prepared.start()
        finally:
            await prepared.aclose()
        await peer.until(
            lambda: all(connection.done.is_set() for connection in peer.accepted)
        )
        assert len(peer.accepted) == capture.value("connections.opened") == 1
        assert capture.value("connections.closed") == 1
        assert capture.value("connections.open") == 0
        assert capture.count("connection.duration") == 1
        assert capture.value("streams.active") == capture.value("streams.pending") == 0
        assert capture.count("stream.duration") == 0
        assert not peer.headers_received


@pytest.mark.anyio
async def test_early_response_close_records_abandonment_and_only_consumed_bytes(
    tmp_path: Path, capture: _Capture
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted, meter_provider=capture.provider
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            response = await client.send(
                client.build_request("POST", peer.origin + "/hold", content=b"request"),
                stream=True,
            )
            assert capture.value("streams.active") == 1
            await response.aclose()
            assert capture.value("streams.active") == 0
            assert capture.value("streams.closed", outcome="abandoned") == 1
            assert capture.count("stream.response.body.size", outcome="abandoned") == 1
            assert capture.total("stream.response.body.size", outcome="abandoned") == 0
            assert capture.total(
                "stream.request.body.size", outcome="abandoned"
            ) == len(peer.operations[0].body)
            assert capture.value("leases") == 0


@pytest.mark.anyio
async def test_peer_stream_limit_updates_are_observed_in_metrics_and_live_spans(
    tmp_path: Path, capture: _Capture
) -> None:
    exporter, tracer = InMemorySpanExporter(), TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    observed = {value: asyncio.Event() for value in (0, 5, None)}

    async def settings_trace(name: str, info: dict[str, object]) -> None:
        event = info.get("return_value")
        if name == "http2.receive_remote_settings.complete" and isinstance(
            event, h2.events.RemoteSettingsChanged
        ):
            change = event.changed_settings.get(
                h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS
            )
            observed[change.new_value if change is not None else None].set()

    try:
        async with (
            HTTP2Peer(tmp_path, max_streams=2) as peer,
            transport.PreparedHTTPTransport(
                peer.origin,
                ssl_context=peer.trusted,
                meter_provider=capture.provider,
                tracer_provider=tracer,
                max_streams=3,
            ) as prepared,
        ):
            assert capture.value("connections.peer_limit", state="unknown") == 1
            assert not peer.headers_received
            async with httpx.AsyncClient(transport=prepared) as client:
                await client.post(peer.origin + "/token", content=b"initial")
                assert capture.value("connections.peer_limit", state="unknown") == 0
                assert capture.value("connections.peer_limit", state="limited") == 1
                assert capture.value("peer.stream_limit.sum") == 2
                assert capture.value("peer.streams.available") == 2
                async with client.stream(
                    "POST",
                    peer.origin + "/hold",
                    content=b"held",
                    extensions={"trace": settings_trace},
                ) as response:
                    consuming = asyncio.create_task(response.aread())
                    try:
                        await peer.settings(
                            1, {h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS: 0}
                        )
                        async with asyncio.timeout(5):
                            await observed[0].wait()
                        assert (
                            capture.value("connections.peer_limit", state="limited")
                            == 1
                        )
                        assert (
                            capture.value("connections.peer_limit", state="unknown")
                            == 0
                        )
                        assert capture.value("peer.stream_limit.sum") == 0
                        assert capture.value("peer.streams.available") == 0
                        await peer.settings(
                            1, {h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS: 5}
                        )
                        async with asyncio.timeout(5):
                            await observed[5].wait()
                        assert capture.value("peer.stream_limit.sum") == 5
                        assert capture.value("peer.streams.available") == 4
                        observed[5].clear()
                        await peer.settings(
                            1, {h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS: 5}
                        )
                        async with asyncio.timeout(5):
                            await observed[5].wait()
                        await peer.settings(
                            1, {h2.settings.SettingCodes.MAX_HEADER_LIST_SIZE: 65536}
                        )
                        async with asyncio.timeout(5):
                            await observed[None].wait()
                        assert capture.value("peer.stream_limit.sum") == 5
                        assert (
                            capture.value(
                                "peer.settings", event="changed", state="limited"
                            )
                            == 2
                        )
                        await peer.release(1, response.extensions["stream_id"])
                        assert await consuming == b"1"
                    finally:
                        consuming.cancel()
                        await asyncio.gather(consuming, return_exceptions=True)
                assert capture.value("peer.streams.available") == 5
                assert prepared.snapshot().available_streams == 3
            assert (
                capture.value("peer.stream_limit.sum")
                == capture.value("peer.streams.available")
                == 0
            )
            assert capture.value("connections.peer_limit", state="limited") == 0
            spans = [
                span
                for span in exporter.get_finished_spans()
                if span.name == "connection.lease"
            ]
            assert [
                span.attributes["http2.peer.max_concurrent_streams"] for span in spans
            ] == [2, 5]
            assert [
                event.attributes["http2.peer.max_concurrent_streams"]
                for event in spans[1].events
                if event.name == "http2.peer_settings"
            ] == [0, 5]
    finally:
        tracer.shutdown()


@pytest.mark.anyio
async def test_omitted_peer_limit_is_not_reported_as_unknown_or_zero_slots(
    tmp_path: Path, capture: _Capture
) -> None:
    exporter, tracer = InMemorySpanExporter(), TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        async with HTTP2Peer(tmp_path, max_streams=None) as peer:
            prepared = transport.PreparedHTTPTransport(
                peer.origin,
                ssl_context=peer.trusted,
                meter_provider=capture.provider,
                tracer_provider=tracer,
            )
            async with httpx.AsyncClient(transport=prepared) as client:
                assert capture.value("connections.peer_limit", state="unknown") == 1
                await client.post(
                    peer.origin + "/token", content=b"no-advertised-limit"
                )
                assert capture.value("connections.peer_limit", state="unknown") == 0
                assert capture.value("connections.peer_limit", state="unlimited") == 1
                assert capture.value("connections.peer_limit", state="limited") == 0
                assert capture.value("peer.stream_limit.sum") == 0
                assert capture.count("peer.stream_limit") == 0
                assert (
                    capture.value("peer.settings", event="initial", state="unlimited")
                    == 1
                )
            assert capture.value("connections.peer_limit", state="unlimited") == 0
            span = next(
                span
                for span in exporter.get_finished_spans()
                if span.name == "connection.lease"
            )
            assert span.attributes["http2.peer.stream_limit.state"] == "unlimited"
            assert "http2.peer.max_concurrent_streams" not in span.attributes
    finally:
        tracer.shutdown()


@pytest.mark.anyio
async def test_peer_limit_rollups_aggregate_same_origin_connections_and_balance_on_close(
    tmp_path: Path, capture: _Capture
) -> None:
    async with HTTP2Peer(tmp_path, max_streams=2) as peer:
        ca_pem = ssl.DER_cert_to_PEM_cert(
            peer.trusted.get_ca_certs(binary_form=True)[0]
        )
        first = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted, meter_provider=capture.provider
        )
        second = transport.PreparedHTTPTransport(
            peer.origin,
            ssl_context=ssl.create_default_context(cadata=ca_pem),
            meter_provider=capture.provider,
        )
        async with httpx.AsyncClient(transport=first) as first_client:
            await first_client.post(peer.origin + "/token", content=b"two-slots")
            peer.max_streams = 6
            async with httpx.AsyncClient(transport=second) as second_client:
                await second_client.post(peer.origin + "/token", content=b"six-slots")
                assert (
                    capture.value("connections.peer_limit", state="limited")
                    == len(peer.accepted)
                    == 2
                )
                assert capture.value("peer.stream_limit.sum") == 8
                assert capture.value("peer.streams.available") == 8
                assert (
                    capture.value("peer.settings", event="initial", state="limited")
                    == 2
                )
            assert capture.value("connections.peer_limit", state="limited") == 1
            assert capture.value("peer.stream_limit.sum") == 2
            assert capture.value("peer.streams.available") == 2
        assert capture.value("connections.peer_limit", state="limited") == 0
        assert (
            capture.value("peer.stream_limit.sum")
            == capture.value("peer.streams.available")
            == 0
        )


@pytest.mark.anyio
async def test_h1_fallback_metrics_report_protocol_and_not_applicable_h2_credits(
    tmp_path: Path, capture: _Capture
) -> None:
    exporter, tracer = InMemorySpanExporter(), TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        async with HTTP2Peer(tmp_path, alpn=None) as peer:
            prepared = transport.PreparedHTTPTransport(
                peer.origin,
                versions=("HTTP/2", "HTTP/1.1"),
                ssl_context=peer.trusted,
                meter_provider=capture.provider,
                tracer_provider=tracer,
            )
            async with httpx.AsyncClient(transport=prepared) as client:
                assert prepared.snapshot().available_streams == 1
                assert (
                    capture.value(
                        "connections.protocol", **{"network.protocol.version": "1.1"}
                    )
                    == 1
                )
                assert (
                    capture.value("connections.peer_limit", state="not_applicable") == 1
                )
                assert capture.value("connections.peer_limit", state="unknown") == 0
                assert capture.value("connections.peer_limit", state="unlimited") == 0
                response = await client.post(peer.origin + "/token", content=b"h1-body")
                assert response.http_version == "HTTP/1.1"
                assert (
                    capture.value(
                        "streams.started", **{"network.protocol.version": "1.1"}
                    )
                    == 1
                )
                assert (
                    capture.value("streams.active")
                    == capture.value("streams.pending")
                    == 0
                )
                assert (
                    capture.value(
                        "streams.closed",
                        outcome="complete",
                        **{"network.protocol.version": "1.1"},
                    )
                    == 1
                )
                assert not capture.points("peer.settings")
            assert (
                capture.value(
                    "connections.protocol", **{"network.protocol.version": "1.1"}
                )
                == 0
            )
            assert capture.value("connections.peer_limit", state="not_applicable") == 0
            span = next(
                span
                for span in exporter.get_finished_spans()
                if span.name == "connection.lease"
            )
            assert span.attributes["network.protocol.version"] == "1.1"
            assert span.attributes["http2.peer.stream_limit.state"] == "not_applicable"
            assert "http2.stream.id" not in span.attributes
    finally:
        tracer.shutdown()
