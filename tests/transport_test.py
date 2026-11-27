import asyncio
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import anyio
import httpx
import pytest
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransport
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from oauthclientbridge import transport
from tests.http2_peer import HTTP2Peer


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_concurrent_retry_replacement_shares_a_different_connection(
    tmp_path: Path,
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            first = await client.post(peer.origin + "/token")
            previous = first.extensions["prepared_connection_id"]
            await asyncio.gather(
                *(prepared.replace_for_retry(previous) for _ in range(4))
            )
            second = await client.post(peer.origin + "/token")
            assert second.extensions["prepared_connection_id"] != previous
            assert len(peer.accepted) == 2


@pytest.mark.anyio
async def test_failed_retry_replacement_never_falls_back_to_previous_connection(
    tmp_path: Path,
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            first = await client.post(peer.origin + "/token")
            peer.context.set_alpn_protocols(["http/1.1"])
            with pytest.raises(httpx.UnsupportedProtocol):
                await prepared.replace_for_retry(
                    first.extensions["prepared_connection_id"]
                )
            with pytest.raises(transport.TransportNotReady):
                await client.post(peer.origin + "/token")
            assert len(peer.operations) == 1


@pytest.mark.anyio
async def test_retry_replacement_respects_draining_two_socket_bound(
    tmp_path: Path,
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            old = await client.send(
                client.build_request("POST", peer.origin + "/hold"), stream=True
            )
            await prepared.cycle()
            current = await client.send(
                client.build_request("POST", peer.origin + "/hold"), stream=True
            )
            try:
                previous = current.extensions["prepared_connection_id"]
                with pytest.raises(transport.TransportNotReady):
                    await prepared.replace_for_retry(previous)
                with pytest.raises(transport.TransportNotReady):
                    await client.post(peer.origin + "/token")
                assert len(peer.accepted) == 2
                await peer.release(1, old.extensions["stream_id"])
                await old.aread()
                await old.aclose()
                await prepared.replace_for_retry(previous)
                replacement = await client.post(peer.origin + "/token")
                assert replacement.extensions["prepared_connection_id"] != previous
                await peer.release(2, current.extensions["stream_id"])
                assert await current.aread() == b"2"
            finally:
                await old.aclose()
                await current.aclose()


@pytest.mark.anyio
async def test_prepares_verified_h2_without_http_and_reuses_exact_connection(
    tmp_path: Path,
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted
        )
        await prepared.start()
        await peer.until(lambda: bool(peer.accepted) and peer.accepted[0].handshake)
        assert peer.accepted[0].alpn == "h2"
        assert peer.accepted[0].plaintext_bytes == 0
        assert not peer.headers_received
        async with httpx.AsyncClient(transport=prepared) as client:
            response = await client.post(
                peer.origin + "/token",
                content=b"grant_type=authorization_code&code=local-only",
            )
            assert response.http_version == "HTTP/2" and response.text == "1"
            assert [operation.connection_id for operation in peer.operations] == [1]
            assert len(peer.accepted) == 1
        await peer.until(lambda: peer.accepted[0].done.is_set())


@pytest.mark.anyio
async def test_replacement_admits_new_streams_while_old_responses_drain(
    tmp_path: Path,
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            first = await client.send(
                client.build_request("POST", peer.origin + "/hold", content=b"first"),
                stream=True,
            )
            second = await client.send(
                client.build_request("POST", peer.origin + "/hold", content=b"second"),
                stream=True,
            )
            stream_ids = [first.extensions["stream_id"], second.extensions["stream_id"]]
            assert len(set(stream_ids)) == 2
            assert [operation.connection_id for operation in peer.operations] == [1, 1]
            await prepared.cycle()
            replacement = await client.post(peer.origin + "/token", content=b"new")
            assert replacement.text == "2"
            await peer.release(1, stream_ids[0])
            assert b"".join([part async for part in first.stream]) == b"1"
            assert not peer.accepted[0].done.is_set()
            await first.aclose()
            assert not peer.accepted[0].done.is_set()
            await peer.release(1, stream_ids[1])
            assert await second.aread() == b"1"
            await second.aclose()
            await peer.until(lambda: peer.accepted[0].done.is_set())
            assert len(peer.accepted) == 2


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.waiters: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds
        for deadline, future in self.waiters.copy():
            if deadline <= self.value and not future.done():
                future.set_result(None)

    async def sleep_until(self, deadline: float) -> None:
        if deadline <= self.value:
            return
        future = asyncio.get_running_loop().create_future()
        item = (deadline, future)
        self.waiters.append(item)
        try:
            await future
        finally:
            self.waiters.remove(item)


class _Journal:
    def __init__(self) -> None:
        self.events: list[transport.TransportEvent] = []
        self.waiters: list[
            tuple[
                Callable[[transport.TransportEvent], bool],
                asyncio.Future[transport.TransportEvent],
            ]
        ] = []

    def __call__(self, event: transport.TransportEvent) -> None:
        self.events.append(event)
        for predicate, future in self.waiters.copy():
            if predicate(event) and not future.done():
                future.set_result(event)

    async def until(self, kind: str, connection_id: int) -> transport.TransportEvent:
        def matches(event: transport.TransportEvent) -> bool:
            return event.kind == kind and event.connection_id == connection_id

        for event in self.events:
            if matches(event):
                return event
        future = asyncio.get_running_loop().create_future()
        item = (matches, future)
        self.waiters.append(item)
        try:
            async with asyncio.timeout(5):
                return await future
        finally:
            self.waiters.remove(item)


@pytest.mark.anyio
async def test_scheduled_preparation_keeps_old_admission_then_discard_refills(
    tmp_path: Path,
) -> None:
    clock, journal = _Clock(), _Journal()
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin,
            ssl_context=peer.trusted,
            max_admission_age=100,
            prepare_ahead=20,
            clock=clock,
            observer=journal,
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            peer.tls_gate.clear()
            clock.advance(80)
            await peer.until(lambda: len(peer.accepted) == 2)
            assert not peer.accepted[1].handshake
            assert (
                await client.post(peer.origin + "/token", content=b"old")
            ).text == "1"
            peer.tls_gate.set()
            await journal.until("published", 2)
            assert peer.accepted[1].plaintext_bytes == 0
            assert (
                await client.post(peer.origin + "/token", content=b"new")
            ).text == "2"
            prepared.discard(2)
            assert prepared.snapshot().current_connection_id is None
            await journal.until("published", 3)
            assert (
                await client.post(peer.origin + "/token", content=b"refilled")
            ).text == "3"


@pytest.mark.anyio
async def test_otel_tracks_each_stream_without_failing_a_healthy_sibling(
    tmp_path: Path,
) -> None:
    reader, exporter = InMemoryMetricReader(), InMemorySpanExporter()
    meter, tracer = MeterProvider(metric_readers=[reader]), TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    journal = _Journal()
    try:
        async with HTTP2Peer(tmp_path) as peer:
            prepared = transport.PreparedHTTPTransport(
                peer.origin,
                ssl_context=peer.trusted,
                observer=journal,
                tracer_provider=tracer,
                meter_provider=meter,
            )
            instrumented = AsyncOpenTelemetryTransport(
                prepared, tracer_provider=tracer, meter_provider=meter
            )
            async with httpx.AsyncClient(transport=instrumented) as client:
                await prepared.start()
                survivor = await client.send(
                    client.build_request(
                        "POST", peer.origin + "/hold", content=b"survive"
                    ),
                    stream=True,
                )
                with pytest.raises(httpx.RemoteProtocolError):
                    await client.post(peer.origin + "/reset", content=b"reset")
                await journal.until("published", 2)
                assert (
                    await client.post(peer.origin + "/token", content=b"new")
                ).text == "2"
                assert prepared.snapshot().active_leases == 1
                await peer.release(1, survivor.extensions["stream_id"])
                assert await survivor.aread() == b"1"
                await survivor.aclose()
            data = reader.get_metrics_data()
            assert data is not None
            points = {
                metric.name: metric.data.data_points
                for resource in data.resource_metrics
                for scope in resource.scope_metrics
                for metric in scope.metrics
                if metric.name.startswith("oauth.transport.")
            }
            assert (
                sum(p.value for p in points["oauth.transport.operation.failures"]) == 1
            )
            assert sum(p.value for p in points["oauth.transport.leases"]) == 0
            assert sum(p.value for p in points["oauth.transport.connections"]) == 0
            assert sum(
                p.count for p in points["oauth.transport.lease.duration"]
            ) == len(peer.operations)
            assert all(
                {"connection.id", "lease.id"}.isdisjoint(p.attributes or {})
                for series in points.values()
                for p in series
            )
            spans = exporter.get_finished_spans()
            old = [
                span
                for span in spans
                if span.name == "connection.lease"
                and span.attributes["connection.id"] == 1
            ]
            assert (
                len(old) == 2
                and sum(
                    span.status.status_code == trace.StatusCode.ERROR for span in old
                )
                == 1
            )
            clients = {
                span.context.span_id
                for span in spans
                if span.kind == trace.SpanKind.CLIENT
            }
            assert all(
                span.parent.span_id in clients
                for span in spans
                if span.name == "connection.lease"
            )
            assert all(
                operation.headers["traceparent"].split("-")[2]
                in {f"{id:016x}" for id in clients}
                for operation in peer.operations
            )
    finally:
        meter.shutdown()
        tracer.shutdown()


@dataclass(frozen=True)
class RejectionCase:
    id: str
    alpn: str
    trust_peer: bool
    error: type[httpx.TransportError]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        RejectionCase(
            id="untrusted-peer", alpn="h2", trust_peer=False, error=httpx.ConnectError
        ),
        RejectionCase(
            id="non-h2-peer",
            alpn="http/1.1",
            trust_peer=True,
            error=httpx.UnsupportedProtocol,
        ),
    ],
    ids=lambda case: case.id,
)
async def test_rejects_untrusted_or_non_h2_peers_without_sending_http(
    tmp_path: Path, case: RejectionCase
) -> None:
    async with HTTP2Peer(tmp_path, alpn=case.alpn) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin,
            ssl_context=peer.trusted
            if case.trust_peer
            else ssl.create_default_context(),
        )
        try:
            with pytest.raises(case.error):
                await prepared.start()
            assert prepared.snapshot().current_connection_id is None
        finally:
            await prepared.aclose()
        await peer.until(
            lambda: bool(peer.accepted) and all(c.done.is_set() for c in peer.accepted)
        )
        assert not peer.headers_received and not peer.operations
        assert all(c.plaintext_bytes == 0 for c in peer.accepted)


@pytest.mark.anyio
async def test_applied_post_response_loss_is_not_replayed_and_refills(
    tmp_path: Path,
) -> None:
    journal = _Journal()
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted, observer=journal
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            with pytest.raises(httpx.RequestError):
                await client.post(
                    peer.origin + "/lost",
                    content=b"grant_type=authorization_code&code=local-only",
                )
            await journal.until("published", 2)
            assert [op.path for op in peer.operations] == ["/lost"]
            assert peer.accepted[1].plaintext_bytes == 0
            assert (
                await client.post(peer.origin + "/distinct", content=b"new-operation")
            ).text == "2"
            assert [op.path for op in peer.operations] == ["/lost", "/distinct"]


@pytest.mark.anyio
async def test_cancelled_replacement_closes_partial_tls_and_keeps_old_admission(
    tmp_path: Path,
) -> None:
    before = asyncio.all_tasks()
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            peer.tls_gate.clear()
            builder = asyncio.create_task(prepared.cycle())
            await peer.until(lambda: len(peer.accepted) == 2)
            await peer.client_hello(2)
            builder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await builder
            assert prepared.snapshot().current_connection_id == 1
            assert not prepared.snapshot().preparing
            assert (
                await client.post(peer.origin + "/token", content=b"old")
            ).text == "1"
            peer.tls_gate.set()
            await peer.until(lambda: peer.accepted[1].done.is_set())
            assert (
                not peer.accepted[1].handshake and peer.accepted[1].plaintext_bytes == 0
            )
            await prepared.cycle()
            assert (
                await client.post(peer.origin + "/token", content=b"new")
            ).text == "3"
    assert asyncio.all_tasks() == before


@pytest.mark.anyio
async def test_anyio_cancelled_response_preserves_admitted_sibling(
    tmp_path: Path,
) -> None:
    journal = _Journal()
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted, observer=journal
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            survivor = await client.send(
                client.build_request("POST", peer.origin + "/hold", content=b"survive"),
                stream=True,
            )
            ready = asyncio.get_running_loop().create_future()

            async def consume() -> None:
                with anyio.CancelScope() as scope:
                    async with client.stream(
                        "POST", peer.origin + "/hold", content=b"cancel"
                    ) as response:
                        ready.set_result(scope)
                        await response.aread()

            task = asyncio.create_task(consume())
            scope = await ready
            scope.cancel()
            await task
            await journal.until("published", 2)
            assert not peer.accepted[0].done.is_set()
            assert (
                await client.post(peer.origin + "/token", content=b"new")
            ).text == "2"
            await peer.release(1, survivor.extensions["stream_id"])
            assert await survivor.aread() == b"1"
            await survivor.aclose()
            assert prepared.snapshot().active_leases == 0


@pytest.mark.anyio
async def test_forced_socket_close_keeps_response_lease_until_failure(
    tmp_path: Path,
) -> None:
    clock, journal = _Clock(), _Journal()
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin,
            ssl_context=peer.trusted,
            drain_timeout=3,
            clock=clock,
            observer=journal,
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            response = await client.send(
                client.build_request("POST", peer.origin + "/hold", content=b"hold"),
                stream=True,
            )
            prepared.discard(1)
            await journal.until("published", 2)
            clock.advance(3)
            await journal.until("forced_closed", 1)
            assert prepared.snapshot().active_leases == 1
            assert all(
                connection.id != 1 for connection in prepared.snapshot().connections
            )
            with pytest.raises(httpx.RequestError):
                await response.aread()
            await response.aclose()
            assert prepared.snapshot().active_leases == 0
            assert (
                await client.post(peer.origin + "/token", content=b"new")
            ).text == "2"


@pytest.mark.anyio
async def test_capacity_and_idle_age_limit_reject_without_origin_http(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin,
            ssl_context=peer.trusted,
            max_streams=2,
            max_admission_age=100,
            prepare_ahead=20,
            clock=clock,
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            responses = [
                await client.send(
                    client.build_request(
                        "POST", peer.origin + "/hold", content=b"hold"
                    ),
                    stream=True,
                )
                for _ in range(2)
            ]
            with pytest.raises(transport.TransportNotReady):
                await client.post(peer.origin + "/saturated", content=b"must-not-send")
            assert len(peer.headers_received) == 2
            for response in responses:
                await peer.release(1, response.extensions["stream_id"])
                await response.aread()
                await response.aclose()
            assert prepared.snapshot().active_leases == 0
            peer.tls_gate.clear()
            clock.advance(100)
            with pytest.raises(transport.TransportNotReady):
                await client.post(peer.origin + "/expired", content=b"must-not-send")
            assert [op.path for op in peer.operations] == ["/hold", "/hold"]


@pytest.mark.anyio
async def test_tls_tickets_h2_control_and_idle_eof_are_not_confused(
    tmp_path: Path,
) -> None:
    journal = _Journal()
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted, observer=journal
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            first = await client.post(peer.origin + "/token", content=b"first")
            ssl_object = first.extensions["network_stream"].get_extra_info("ssl_object")
            assert ssl_object.version() == "TLSv1.3" and ssl_object.session.has_ticket
            await peer.ping(1)
            assert (
                await client.post(peer.origin + "/token", content=b"second")
            ).text == "1"
            await peer.until(lambda: peer.accepted[0].ping_acks == 1)
            assert len(peer.accepted) == 1
            peer.accepted[0].writer.transport.abort()
            await journal.until("published", 2)
            assert peer.accepted[1].plaintext_bytes == 0
            assert (
                await client.post(peer.origin + "/token", content=b"refill")
            ).text == "2"


@pytest.mark.anyio
async def test_concurrent_shutdown_drains_even_if_one_waiter_is_cancelled(
    tmp_path: Path,
) -> None:
    before = asyncio.all_tasks()
    journal = _Journal()
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted, observer=journal
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            response = await client.send(
                client.build_request("POST", peer.origin + "/hold", content=b"hold"),
                stream=True,
            )
            first, second = (
                asyncio.create_task(prepared.aclose()),
                asyncio.create_task(prepared.aclose()),
            )
            await journal.until("retired", 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not second.done()
            await peer.release(1, response.extensions["stream_id"])
            assert await response.aread() == b"1"
            await response.aclose()
            await second
            await peer.until(lambda: peer.accepted[0].done.is_set())
    assert asyncio.all_tasks() == before


@pytest.mark.anyio
async def test_observer_errors_do_not_break_progress_or_log_contents(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken_observer(event: transport.TransportEvent) -> None:
        raise RuntimeError("sensitive-observer-error-content")

    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted, observer=broken_observer
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            assert (
                await client.post(peer.origin + "/token", content=b"first")
            ).text == "1"
            await prepared.cycle()
            assert (
                await client.post(peer.origin + "/token", content=b"second")
            ).text == "2"
    assert "Transport observer failed" in caplog.text
    assert "sensitive-observer-error-content" not in caplog.text


@pytest.mark.anyio
async def test_idle_goaway_fails_without_reassigning_or_replaying_the_post(
    tmp_path: Path,
) -> None:
    journal = _Journal()
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted, observer=journal
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            await client.post(peer.origin + "/token", content=b"first")
            await peer.goaway(1)
            with pytest.raises(httpx.RequestError):
                await client.post(
                    peer.origin + "/after-goaway", content=b"must-not-replay"
                )
            await journal.until("published", 2)
            assert (
                await client.post(peer.origin + "/distinct", content=b"new")
            ).text == "2"
            assert [op.path for op in peer.operations] == ["/token", "/distinct"]


@pytest.mark.anyio
async def test_inventory_aggregates_transports_sharing_an_origin_and_meter(
    tmp_path: Path,
) -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader])
    try:
        async with HTTP2Peer(tmp_path) as peer:
            ca_pem = ssl.DER_cert_to_PEM_cert(
                peer.trusted.get_ca_certs(binary_form=True)[0]
            )
            first = transport.PreparedHTTPTransport(
                peer.origin, ssl_context=peer.trusted, meter_provider=meter
            )
            second = transport.PreparedHTTPTransport(
                peer.origin,
                ssl_context=ssl.create_default_context(cadata=ca_pem),
                meter_provider=meter,
            )
            try:
                await first.start()
                await second.start()

                def ready_count() -> int:
                    data = reader.get_metrics_data()
                    assert data is not None
                    return sum(
                        point.value
                        for resource in data.resource_metrics
                        for scope in resource.scope_metrics
                        for metric in scope.metrics
                        if metric.name == "oauth.transport.connections"
                        for point in metric.data.data_points
                        if point.attributes["state"] == "ready"
                    )

                assert ready_count() == len(peer.accepted) == 2
                await first.aclose()
                assert ready_count() == 1
                await second.aclose()
                assert ready_count() == 0
            finally:
                await first.aclose()
                await second.aclose()
    finally:
        meter.shutdown()


@pytest.mark.anyio
async def test_shutdown_cancels_scheduled_tls_preparation_without_leaking_tasks(
    tmp_path: Path,
) -> None:
    before = asyncio.all_tasks()
    clock = _Clock()
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin,
            ssl_context=peer.trusted,
            max_admission_age=100,
            prepare_ahead=20,
            clock=clock,
        )
        await prepared.start()
        peer.tls_gate.clear()
        clock.advance(80)
        await peer.until(lambda: len(peer.accepted) == 2)
        await peer.client_hello(2)
        await prepared.aclose()
        peer.tls_gate.set()
        await peer.until(
            lambda: all(connection.done.is_set() for connection in peer.accepted)
        )
        assert not peer.accepted[1].handshake
        assert not peer.headers_received
        assert not prepared.snapshot().connections
        assert not prepared.snapshot().preparing
    assert asyncio.all_tasks() == before


@pytest.mark.anyio
async def test_closed_connection_fails_credit_waiter_without_waiting_for_held_response(
    tmp_path: Path,
) -> None:
    clock, journal = _Clock(), _Journal()
    admitted = asyncio.Event()

    def observe(event: transport.TransportEvent) -> None:
        journal(event)
        if event.kind == "acquired" and event.state.active_leases == 2:
            admitted.set()

    async with HTTP2Peer(tmp_path, max_streams=1) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin,
            ssl_context=peer.trusted,
            max_streams=2,
            drain_timeout=3,
            clock=clock,
            observer=observe,
        )
        async with (
            httpx.AsyncClient(transport=prepared) as client,
            client.stream("POST", peer.origin + "/hold", content=b"held") as first,
        ):
            second = asyncio.create_task(
                client.post(peer.origin + "/queued", content=b"must-not-replay")
            )
            try:
                async with asyncio.timeout(5):
                    await admitted.wait()
                prepared.discard(1)
                await journal.until("published", 2)
                clock.advance(3)
                await journal.until("forced_closed", 1)
                await prepared.aclose()
                await peer.until(lambda: peer.accepted[0].done.is_set())
                async with asyncio.timeout(5):
                    with pytest.raises(httpx.RequestError):
                        await second
                assert not first.is_closed
                assert prepared.snapshot().active_leases == 1
                assert [operation.path for operation in peer.operations] == ["/hold"]
            finally:
                second.cancel()
                await asyncio.gather(second, return_exceptions=True)
        assert prepared.snapshot().active_leases == 0


@pytest.mark.anyio
async def test_version_preferences_prepare_and_reuse_h1_without_a_probe_request(
    tmp_path: Path,
) -> None:
    async with HTTP2Peer(tmp_path, alpn="http/1.1") as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, versions=("HTTP/2", "HTTP/1.1"), ssl_context=peer.trusted
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            await peer.until(lambda: bool(peer.accepted) and peer.accepted[0].handshake)
            assert peer.accepted[0].alpn == "http/1.1"
            assert peer.accepted[0].plaintext_bytes == 0 and not peer.headers_received
            first = await client.post(peer.origin + "/token", content=b"first")
            second = await client.post(peer.origin + "/token", content=b"second")
            assert first.http_version == second.http_version == "HTTP/1.1"
            assert first.text == second.text == "1"
            assert "stream_id" not in first.extensions
            assert [operation.connection_id for operation in peer.operations] == [1, 1]
            assert len(peer.accepted) == 1


@dataclass(frozen=True)
class NegotiationCase:
    id: str
    versions: tuple[transport.HTTPVersion, ...]
    server_protocols: tuple[str, ...]
    expected: transport.HTTPVersion


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        NegotiationCase(
            id="prefer-h2",
            versions=("HTTP/2", "HTTP/1.1"),
            server_protocols=("h2", "http/1.1"),
            expected="HTTP/2",
        ),
        NegotiationCase(
            id="server-prefers-h1",
            versions=("HTTP/2", "HTTP/1.1"),
            server_protocols=("http/1.1", "h2"),
            expected="HTTP/1.1",
        ),
        NegotiationCase(
            id="order-is-not-probing",
            versions=("HTTP/1.1", "HTTP/2"),
            server_protocols=("h2", "http/1.1"),
            expected="HTTP/2",
        ),
        NegotiationCase(
            id="h1-only",
            versions=("HTTP/1.1",),
            server_protocols=("h2", "http/1.1"),
            expected="HTTP/1.1",
        ),
        NegotiationCase(
            id="no-alpn",
            versions=("HTTP/2", "HTTP/1.1"),
            server_protocols=(),
            expected="HTTP/1.1",
        ),
    ],
    ids=lambda case: case.id,
)
async def test_version_tuple_negotiates_once_before_any_http(
    tmp_path: Path, case: NegotiationCase
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        peer.context.set_alpn_protocols(list(case.server_protocols))
        prepared = transport.PreparedHTTPTransport(
            peer.origin, versions=case.versions, ssl_context=peer.trusted
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            await peer.until(lambda: bool(peer.accepted) and peer.accepted[0].handshake)
            assert not peer.headers_received and peer.accepted[0].plaintext_bytes == 0
            assert prepared.snapshot().connections[0].version == case.expected
            response = await client.post(
                peer.origin + "/token", content=b"one-operation"
            )
            assert response.http_version == case.expected and response.text == "1"
            assert len(peer.accepted) == len(peer.operations) == 1
            if not case.server_protocols:
                assert response.extensions["prepared_readiness"] == "tls_http1_default"


@pytest.mark.anyio
async def test_h2_streams_drain_while_h1_replacement_admits_one_operation(
    tmp_path: Path,
) -> None:
    async with HTTP2Peer(tmp_path) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin,
            versions=("HTTP/2", "HTTP/1.1"),
            ssl_context=peer.trusted,
            max_streams=3,
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            old = [
                await client.send(
                    client.build_request("POST", peer.origin + "/hold", content=b"old"),
                    stream=True,
                )
                for _ in range(2)
            ]
            peer.context.set_alpn_protocols(["http/1.1"])
            await prepared.cycle()
            assert prepared.snapshot().available_streams == 1
            async with client.stream(
                "POST", peer.origin + "/hold", content=b"new"
            ) as replacement:
                assert replacement.http_version == "HTTP/1.1"
                with pytest.raises(transport.TransportNotReady):
                    await client.post(
                        peer.origin + "/saturated", content=b"must-not-send"
                    )
                assert len(peer.operations) == 3
                for response in old:
                    await peer.release(1, response.extensions["stream_id"])
                    assert await response.aread() == b"1"
                    await response.aclose()
                await peer.until(lambda: peer.accepted[0].done.is_set())
                assert not peer.accepted[1].done.is_set()
                await peer.release(2, 0)
                assert await replacement.aread() == b"2"
            assert prepared.snapshot().available_streams == 1


@dataclass(frozen=True)
class ProtocolFailureCase:
    id: str
    alpn: str | None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        ProtocolFailureCase(id="h2", alpn="h2"),
        ProtocolFailureCase(id="h1", alpn="http/1.1"),
        ProtocolFailureCase(id="no-alpn", alpn=None),
    ],
    ids=lambda case: case.id,
)
async def test_negotiated_protocol_failure_never_replays_on_another_version(
    tmp_path: Path, case: ProtocolFailureCase
) -> None:
    journal = _Journal()
    async with HTTP2Peer(tmp_path, alpn=case.alpn) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin,
            versions=("HTTP/2", "HTTP/1.1"),
            ssl_context=peer.trusted,
            observer=journal,
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            with pytest.raises(httpx.RequestError):
                await client.post(
                    peer.origin + "/lost",
                    content=b"grant_type=authorization_code&code=local-only",
                )
            await journal.until("published", 2)
            assert [operation.path for operation in peer.operations] == ["/lost"]
            response = await client.post(
                peer.origin + "/distinct", content=b"different-operation"
            )
            assert response.text == "2"
            assert [operation.path for operation in peer.operations] == [
                "/lost",
                "/distinct",
            ]


@pytest.mark.anyio
async def test_fallback_does_not_relax_certificate_verification(tmp_path: Path) -> None:
    async with HTTP2Peer(tmp_path, alpn="http/1.1") as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, versions=("HTTP/2", "HTTP/1.1")
        )
        try:
            with pytest.raises(httpx.ConnectError):
                await prepared.start()
        finally:
            await prepared.aclose()
        await peer.until(
            lambda: (
                bool(peer.accepted)
                and all(connection.done.is_set() for connection in peer.accepted)
            )
        )
        assert len(peer.accepted) == 1
        assert not peer.headers_received and not peer.operations


@dataclass(frozen=True)
class InvalidVersionsCase:
    id: str
    versions: tuple[str, ...]


@pytest.mark.parametrize(
    "case",
    [
        InvalidVersionsCase(id="empty", versions=()),
        InvalidVersionsCase(id="duplicate", versions=("HTTP/2", "HTTP/2")),
        InvalidVersionsCase(id="unsupported", versions=("HTTP/3",)),
    ],
    ids=lambda case: case.id,
)
def test_version_preferences_reject_empty_duplicate_or_unsupported_values(
    case: InvalidVersionsCase,
) -> None:
    with pytest.raises(ValueError, match="versions"):
        transport.PreparedHTTPTransport(
            "https://provider.example",
            versions=cast(tuple[transport.HTTPVersion, ...], case.versions),
        )


@pytest.mark.anyio
async def test_planned_retirement_keeps_already_admitted_credit_waiter_on_old_connection(
    tmp_path: Path,
) -> None:
    admitted = asyncio.Event()

    def observe(event: transport.TransportEvent) -> None:
        if event.kind == "acquired" and event.state.active_leases == 2:
            admitted.set()

    async with HTTP2Peer(tmp_path, max_streams=1) as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, ssl_context=peer.trusted, max_streams=2, observer=observe
        )
        async with (
            httpx.AsyncClient(transport=prepared) as client,
            client.stream("POST", peer.origin + "/hold", content=b"held") as first,
        ):
            waiting = asyncio.create_task(
                client.post(peer.origin + "/old-queued", content=b"admitted")
            )
            try:
                async with asyncio.timeout(5):
                    await admitted.wait()
                await prepared.cycle()
                assert (
                    await client.post(peer.origin + "/new", content=b"new")
                ).text == "2"
                assert not waiting.done()
                await peer.release(1, first.extensions["stream_id"])
                assert await first.aread() == b"1"
                await first.aclose()
                async with asyncio.timeout(5):
                    assert (await waiting).text == "1"
                assert [(op.path, op.connection_id) for op in peer.operations] == [
                    ("/hold", 1),
                    ("/new", 2),
                    ("/old-queued", 1),
                ]
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.anyio
async def test_h1_peer_closure_does_not_discard_an_already_buffered_response(
    tmp_path: Path,
) -> None:
    async with HTTP2Peer(tmp_path, alpn="http/1.1") as peer:
        prepared = transport.PreparedHTTPTransport(
            peer.origin, versions=("HTTP/2", "HTTP/1.1"), ssl_context=peer.trusted
        )
        async with httpx.AsyncClient(transport=prepared) as client:
            response = await client.post(
                peer.origin + "/token", content=b"once", headers={"connection": "close"}
            )
            assert response.http_version == "HTTP/1.1" and response.text == "1"
            assert [operation.path for operation in peer.operations] == ["/token"]
