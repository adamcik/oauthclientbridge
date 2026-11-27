"""Connection and stream measurements derived from transport lifecycle events.

Up/down deltas aggregate transports sharing a meter without exporter-thread reads
of mutable protocol state. Identities belong on spans, not metric labels. Stream
allocation, socket cleanup, and caller response closure are separate events;
none proves that an upstream operation was applied.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Literal

import h2.events
import h2.settings
from opentelemetry import metrics, trace
from opentelemetry.context import Context

from ._models import EventKind, HTTPVersion, Phase, Reason, TransportState

_DURATION_BUCKETS = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1,
    2.5,
    5,
    10,
    30,
    60,
    120,
    600,
)
_BODY_BUCKETS = (0, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576)
_CONNECTION_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600)

type StreamOutcome = Literal["complete", "abandoned", "cancelled", "failed"]
type PeerLimitState = Literal["unknown", "unlimited", "limited", "not_applicable"]


class Diagnostics:
    def __init__(
        self,
        host: str,
        port: int,
        max_age: float,
        tracer_provider: trace.TracerProvider | None,
        meter_provider: metrics.MeterProvider | None,
    ) -> None:
        self._attributes: dict[str, str | int] = {
            "server.address": host,
            "server.port": port,
        }
        self._max_age = max_age
        self._tracer = trace.get_tracer(
            "oauthclientbridge.transport", tracer_provider=tracer_provider
        )
        meter = metrics.get_meter(
            "oauthclientbridge.transport", meter_provider=meter_provider
        )
        self._events = meter.create_counter(
            "oauth.transport.connection.events", unit="{event}"
        )
        self._phases = meter.create_counter(
            "oauth.transport.setup.events", unit="{event}"
        )
        self._setup = meter.create_histogram(
            "oauth.transport.setup.duration",
            unit="s",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        )
        self._drain = meter.create_histogram(
            "oauth.transport.drain.duration",
            unit="s",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        )
        self._admissions = meter.create_counter(
            "oauth.transport.admissions", unit="{operation}"
        )
        self._failures = meter.create_counter(
            "oauth.transport.operation.failures", unit="{operation}"
        )
        self._duration = meter.create_histogram(
            "oauth.transport.lease.duration",
            unit="s",
            description="Admission through response closure/failure, not just headers",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        )
        self._inventory = meter.create_up_down_counter(
            "oauth.transport.connections",
            unit="{connection}",
            description="Connection slots by state; preparing counts attempts, not established TCP sockets; busy is not saturated",
        )
        self._leases = meter.create_up_down_counter(
            "oauth.transport.leases", unit="{lease}"
        )
        self._capacity = meter.create_up_down_counter(
            "oauth.transport.stream_capacity",
            unit="{lease}",
            description="Last observed local admission allowance, not peer SETTINGS or future liveness",
        )
        self._stream_instruments = _StreamInstruments(
            attributes=self._attributes,
            active=meter.create_up_down_counter(
                "oauth.transport.streams.active",
                unit="{stream}",
                description="Client-owned HTTP exchanges/streams until HTTPcore release or socket closure; not peer receipt",
            ),
            pending=meter.create_up_down_counter(
                "oauth.transport.streams.pending",
                unit="{operation}",
                description="Admitted operations awaiting stream allocation, including initialization and peer credit",
            ),
            started=meter.create_counter(
                "oauth.transport.streams.started",
                unit="{stream}",
                description="Client stream allocations, not proof of request receipt",
            ),
            closed=meter.create_counter(
                "oauth.transport.streams.closed",
                unit="{stream}",
                description="Closed client streams with final caller outcomes; complete means full consumption, not HTTP or OAuth success",
            ),
            duration=meter.create_histogram(
                "oauth.transport.stream.duration",
                unit="s",
                description="Allocation through HTTPcore release or socket closure, recorded with final outcome",
                explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
            ),
            waiting=meter.create_histogram(
                "oauth.transport.stream.wait.duration",
                unit="s",
                description="Admission through stream allocation or cancellation/failure before allocation",
                explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
            ),
            request_body=meter.create_histogram(
                "oauth.transport.stream.request.body.size",
                unit="By",
                description="Body bytes offered to HTTPcore for an allocated stream; not proof of transmission or receipt",
                explicit_bucket_boundaries_advisory=_BODY_BUCKETS,
            ),
            response_body=meter.create_histogram(
                "oauth.transport.stream.response.body.size",
                unit="By",
                description="Raw body bytes yielded by the transport before HTTPX content decoding; may be partial",
                explicit_bucket_boundaries_advisory=_BODY_BUCKETS,
            ),
        )
        self._stream_instruments.active.add(0, self._attributes)
        self._stream_instruments.pending.add(0, self._attributes)
        self._network_instruments = _NetworkInstruments(
            attributes=self._attributes,
            opened=meter.create_counter(
                "oauth.transport.connections.opened",
                unit="{connection}",
                description="Completed TCP connections, including candidates that later fail TLS",
            ),
            closed=meter.create_counter(
                "oauth.transport.connections.closed",
                unit="{connection}",
                description="Established TCP connections whose owned cleanup completed",
            ),
            current=meter.create_up_down_counter(
                "oauth.transport.connections.open",
                unit="{connection}",
                description="Established TCP resources still owned locally, including unusable peers until cleanup; not readiness",
            ),
            duration=meter.create_histogram(
                "oauth.transport.connection.duration",
                unit="s",
                description="TCP establishment through completed local cleanup, including failed TLS preparation",
                explicit_bucket_boundaries_advisory=_CONNECTION_BUCKETS,
            ),
            streams=meter.create_histogram(
                "oauth.transport.connection.streams",
                unit="{stream}",
                description="Client stream allocations before a TCP resource closed; not peer receipt",
                explicit_bucket_boundaries_advisory=(
                    0,
                    1,
                    2,
                    4,
                    8,
                    16,
                    32,
                    64,
                    128,
                    256,
                    512,
                    1024,
                ),
            ),
            io=meter.create_counter(
                "oauth.transport.connection.io",
                unit="By",
                description="HTTP plaintext from completed TLS reads/writes, including control frames; excludes TLS/TCP overhead and unknown partial writes",
            ),
            protocol=meter.create_up_down_counter(
                "oauth.transport.connections.protocol",
                unit="{connection}",
                description="Owned TCP resources by negotiated protocol; unknown while TLS negotiation is incomplete",
            ),
        )
        self._network_instruments.current.add(0, self._attributes)
        for version in ("unknown", "1.1", "2"):
            self._network_instruments.protocol.add(
                0, self._attributes | {"network.protocol.version": version}
            )
        for direction in ("sent", "received"):
            self._network_instruments.io.add(
                0, self._attributes | {"direction": direction}
            )
        self._peer_instruments = _PeerInstruments(
            attributes=self._attributes,
            connections=meter.create_up_down_counter(
                "oauth.transport.connections.peer_limit",
                unit="{connection}",
                description="Owned TCP connections by peer-ceiling knowledge; unknown includes unfinished H2 setup, not_applicable identifies H1",
            ),
            limits=meter.create_up_down_counter(
                "oauth.transport.peer.stream_limit.sum",
                unit="{stream}",
                description="Sum of last observed finite peer ceilings across owned connections, including retiring ones; consult knowledge counts for unknown/unlimited",
            ),
            available=meter.create_up_down_counter(
                "oauth.transport.peer.streams.available",
                unit="{stream}",
                description="Sum of estimated finite peer ceilings minus client-owned streams; not admission capacity, reservations, or HTTPcore effective limits",
            ),
            updates=meter.create_counter(
                "oauth.transport.peer.settings",
                unit="{event}",
                description="Initial peer-ceiling observations and subsequent ceiling changes; unrelated SETTINGS do not count",
            ),
            observed_limit=meter.create_histogram(
                "oauth.transport.peer.stream_limit",
                unit="{stream}",
                description="Finite ceilings observed in initial or changed peer SETTINGS, not a current-capacity gauge",
                explicit_bucket_boundaries_advisory=(
                    0,
                    1,
                    2,
                    4,
                    8,
                    16,
                    32,
                    64,
                    100,
                    128,
                    256,
                    512,
                    1024,
                    4096,
                    65536,
                ),
            ),
        )
        for state in ("unknown", "unlimited", "limited", "not_applicable"):
            self._peer_instruments.connections.add(
                0, self._attributes | {"state": state}
            )
        self._peer_instruments.limits.add(0, self._attributes)
        self._peer_instruments.available.add(0, self._attributes)
        self._counts = dict.fromkeys(
            ("ready", "busy", "draining", "expired", "preparing"), 0
        )
        self._lease_count = self._available = 0

    def observe(self, state: TransportState) -> None:
        counts = dict.fromkeys(self._counts, 0)
        counts["preparing"] = int(state.preparing)
        for connection in state.connections:
            kind = (
                "draining"
                if connection.retiring
                else "expired"
                if connection.admission_age >= self._max_age
                else "busy"
                if connection.leases
                else "ready"
            )
            counts[kind] += 1
        for kind, count in counts.items():
            self._inventory.add(
                count - self._counts[kind], self._attributes | {"state": kind}
            )
        self._leases.add(state.active_leases - self._lease_count, self._attributes)
        self._capacity.add(state.available_streams - self._available, self._attributes)
        self._counts = counts
        self._lease_count, self._available = (
            state.active_leases,
            state.available_streams,
        )

    def event(self, kind: EventKind, reason: Reason) -> None:
        self._events.add(1, self._attributes | {"event": kind, "reason": reason})

    def admission(
        self,
        outcome: Literal[
            "origin_mismatch",
            "unavailable",
            "expired",
            "saturated",
            "unhealthy",
            "admitted",
        ],
    ) -> None:
        self._admissions.add(1, self._attributes | {"outcome": outcome})

    def connection(self) -> "ConnectionMeasurements":
        return ConnectionMeasurements(
            self._stream_instruments, self._network_instruments, self._peer_instruments
        )

    @contextmanager
    def preparing(self, connection_id: int, reason: Reason) -> Generator[trace.Span]:
        with self._tracer.start_as_current_span(
            "connection.prepare",
            context=Context(),
            attributes=self._attributes
            | {
                "connection.id": connection_id,
                "reason": reason,
                "readiness": "tls_protocol",
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException as exc:
                self._error(span, exc)
                raise

    @asynccontextmanager
    async def phase(self, phase: Phase) -> AsyncGenerator[None]:
        started = time.perf_counter()
        self._phases.add(1, self._attributes | {"phase": phase, "event": "started"})
        outcome = "complete"
        with self._tracer.start_as_current_span(
            "connection." + phase, record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                yield
            except BaseException as exc:
                outcome = (
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                )
                self._error(span, exc)
                raise
            finally:
                self._phases.add(
                    1, self._attributes | {"phase": phase, "event": outcome}
                )
                self._setup.record(
                    time.perf_counter() - started,
                    self._attributes | {"phase": phase, "outcome": outcome},
                )

    def lease(self, connection_id: int, lease_id: int) -> trace.Span:
        return self._tracer.start_span(
            "connection.lease",
            attributes=self._attributes
            | {"connection.id": connection_id, "lease.id": lease_id},
        )

    def failed(
        self,
        span: trace.Span,
        exc: BaseException,
        outcome: Literal["cancelled", "failed"],
    ) -> None:
        self._failures.add(1, self._attributes | {"outcome": outcome})
        self._error(span, exc)

    def released(
        self, span: trace.Span, started: float, outcome: StreamOutcome
    ) -> None:
        self._duration.record(
            time.perf_counter() - started, self._attributes | {"outcome": outcome}
        )
        span.end()

    def drained(self, seconds: float, reason: Reason, forced: bool) -> None:
        self._drain.record(
            max(0, seconds), self._attributes | {"reason": reason, "forced": forced}
        )

    @staticmethod
    def _error(span: trace.Span, exc: BaseException) -> None:
        span.set_status(trace.StatusCode.ERROR)
        span.set_attribute("error.type", type(exc).__name__)


@dataclass(frozen=True)
class _StreamInstruments:
    attributes: dict[str, str | int]
    active: metrics.UpDownCounter
    pending: metrics.UpDownCounter
    started: metrics.Counter
    closed: metrics.Counter
    duration: metrics.Histogram
    waiting: metrics.Histogram
    request_body: metrics.Histogram
    response_body: metrics.Histogram


@dataclass(frozen=True)
class _NetworkInstruments:
    attributes: dict[str, str | int]
    opened: metrics.Counter
    closed: metrics.Counter
    current: metrics.UpDownCounter
    duration: metrics.Histogram
    streams: metrics.Histogram
    io: metrics.Counter
    protocol: metrics.UpDownCounter


@dataclass(frozen=True)
class _PeerInstruments:
    attributes: dict[str, str | int]
    connections: metrics.UpDownCounter
    limits: metrics.UpDownCounter
    available: metrics.UpDownCounter
    updates: metrics.Counter
    observed_limit: metrics.Histogram


class ConnectionMeasurements:
    """Measurements for one TCP resource and its client-owned streams."""

    def __init__(
        self,
        instruments: _StreamInstruments,
        network: _NetworkInstruments,
        peer: _PeerInstruments,
    ) -> None:
        self.instruments = instruments
        self._network = network
        self._peer = peer
        self._connected: float | None = None
        self._recorded_close = False
        self._allocations = 0
        self.closed = False
        self.streams: set[StreamMeasurements] = set()
        self._operations: set[StreamMeasurements] = set()
        self._peer_state: PeerLimitState = "unknown"
        self._version: HTTPVersion | None = None
        self._reported_protocol: str | None = None
        self._peer_limit: int | None = None
        self._reported_state: PeerLimitState | None = None
        self._reported_limit = self._reported_available = 0

    def operation(self, span: trace.Span, admitted: float) -> "StreamMeasurements":
        operation = StreamMeasurements(self, span, admitted)
        self._operations.add(operation)
        self._annotate(operation)
        return operation

    def connected(self) -> None:
        if self._connected is not None:
            return
        self._connected = time.perf_counter()
        self._network.opened.add(1, self._network.attributes)
        self._network.current.add(1, self._network.attributes)
        self._observe_peer()

    def negotiated(self, version: HTTPVersion) -> None:
        self._version = version
        if version == "HTTP/1.1":
            self._peer_state = "not_applicable"
        self._observe_peer()

    def operation_attributes(self) -> dict[str, str | int]:
        protocol = (
            "2"
            if self._version == "HTTP/2"
            else "1.1"
            if self._version == "HTTP/1.1"
            else "unknown"
        )
        return self.instruments.attributes | {"network.protocol.version": protocol}

    def transferred(self, direction: Literal["sent", "received"], size: int) -> None:
        self._network.io.add(size, self._network.attributes | {"direction": direction})

    def allocated(self, stream: "StreamMeasurements") -> None:
        self.streams.add(stream)
        self._allocations += 1
        self._observe_peer()

    def released(self, stream: "StreamMeasurements") -> None:
        self.streams.remove(stream)
        self._observe_peer()

    def finished(self, operation: "StreamMeasurements") -> None:
        self._operations.discard(operation)

    def settings(self, event: h2.events.RemoteSettingsChanged) -> None:
        if self.closed or self._version != "HTTP/2":
            return
        changed = event.changed_settings.get(
            h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS
        )
        if changed is None and self._peer_state != "unknown":
            return
        state: PeerLimitState = "unlimited" if changed is None else "limited"
        limit = changed.new_value if changed is not None else None
        if (state, limit) == (self._peer_state, self._peer_limit):
            return
        kind = "initial" if self._peer_state == "unknown" else "changed"
        previous = self._peer_limit
        self._peer_state, self._peer_limit = state, limit
        self._peer.updates.add(
            1, self._peer.attributes | {"event": kind, "state": state}
        )
        if limit is not None:
            self._peer.observed_limit.record(
                limit, self._peer.attributes | {"event": kind}
            )
        self._observe_peer(changed=True, previous=previous)

    def _observe_peer(
        self, *, changed: bool = False, previous: int | None = None
    ) -> None:
        # Unknown and omitted limits contribute no finite value; their separate
        # connection counts distinguish them from an explicitly advertised zero.
        owned = self._connected is not None and not self.closed
        protocol = (
            str(self.operation_attributes()["network.protocol.version"])
            if owned
            else None
        )
        if protocol != self._reported_protocol:
            if self._reported_protocol is not None:
                self._network.protocol.add(
                    -1,
                    self._network.attributes
                    | {"network.protocol.version": self._reported_protocol},
                )
            if protocol is not None:
                self._network.protocol.add(
                    1, self._network.attributes | {"network.protocol.version": protocol}
                )
            self._reported_protocol = protocol
        state = self._peer_state if owned else None
        if state != self._reported_state:
            if self._reported_state is not None:
                self._peer.connections.add(
                    -1, self._peer.attributes | {"state": self._reported_state}
                )
            if state is not None:
                self._peer.connections.add(1, self._peer.attributes | {"state": state})
        limit = self._peer_limit if owned and self._peer_limit is not None else 0
        available = (
            max(0, limit - len(self.streams))
            if owned and self._peer_limit is not None
            else 0
        )
        self._peer.limits.add(limit - self._reported_limit, self._peer.attributes)
        self._peer.available.add(
            available - self._reported_available, self._peer.attributes
        )
        self._reported_state, self._reported_limit, self._reported_available = (
            state,
            limit,
            available,
        )
        for operation in self._operations.copy():
            self._annotate(operation, changed=changed, previous=previous)

    def _annotate(
        self,
        operation: "StreamMeasurements",
        *,
        changed: bool = False,
        previous: int | None = None,
    ) -> None:
        available = (
            (max(0, self._peer_limit - len(self.streams)) if not self.closed else 0)
            if self._peer_limit is not None
            else None
        )
        operation.peer_settings(
            self._peer_state,
            self._peer_limit,
            available,
            closed=self.closed,
            changed=changed,
            previous=previous,
            version=self._version,
        )

    def close(self) -> None:
        self.closed = True
        for stream in self.streams.copy():
            stream.protocol_closed()
        if self._connected is not None and not self._recorded_close:
            self._recorded_close = True
            self._network.current.add(-1, self._network.attributes)
            self._network.closed.add(1, self._network.attributes)
            self._network.duration.record(
                time.perf_counter() - self._connected, self._network.attributes
            )
            self._network.streams.record(self._allocations, self._network.attributes)
        self._observe_peer()


class StreamMeasurements:
    """Measure stream allocation separately from caller response ownership.

    Allocation and release describe client ownership, not peer stream state.
    Final outcomes and body sizes are recorded after the caller's lease ends
    and either HTTPcore releases the stream or the socket is cleaned up.
    """

    def __init__(
        self, connection: ConnectionMeasurements, span: trace.Span, admitted: float
    ) -> None:
        self._connection, self._span, self._admitted = connection, span, admitted
        self._instruments = connection.instruments
        self._attributes = connection.operation_attributes()
        self._stream_id: int | None = None
        self._allocated: float | None = None
        self._ended: float | None = None
        self._pending = True
        self._finished = False
        self._recorded = False
        self._outcome: StreamOutcome | None = None
        self._request_bytes = self._response_bytes = 0
        self._instruments.pending.add(1, self._attributes)

    def trace(self, name: str, info: dict[str, object]) -> None:
        # HTTPcore 1.0.9's public trace vocabulary is version-sensitive. The
        # integration tests reconcile these observations with the real peer.
        if name == "http2.receive_remote_settings.complete":
            event = info.get("return_value")
            if isinstance(event, h2.events.RemoteSettingsChanged):
                self._connection.settings(event)
        elif (
            name
            in (
                "http2.send_request_headers.started",
                "http11.send_request_headers.started",
            )
            and self._allocated is None
        ):
            stream_id = info.get("stream_id")
            if name.startswith("http11.") or isinstance(stream_id, int):
                if isinstance(stream_id, int):
                    self._stream_id = stream_id
                    self._span.set_attribute("http2.stream.id", stream_id)
                self._allocated = time.perf_counter()
                self._end_wait("allocated")
                self._instruments.started.add(1, self._attributes)
                self._instruments.active.add(1, self._attributes)
                self._connection.allocated(self)
                if self._connection.closed:
                    self.protocol_closed()
        elif name in (
            "http2.response_closed.complete",
            "http11.response_closed.complete",
        ):
            self.protocol_closed()

    def protocol_closed(self) -> None:
        if self not in self._connection.streams:
            return
        self._ended = time.perf_counter()
        self._connection.released(self)
        self._instruments.active.add(-1, self._attributes)
        self._record_finished()

    def request_chunk(self, size: int) -> None:
        self._request_bytes += size

    def response_chunk(self, size: int) -> None:
        self._response_bytes += size

    def finish(self, outcome: StreamOutcome) -> None:
        if self._finished:
            return
        self._finished = True
        self._connection.finished(self)
        self._outcome = outcome
        if self._allocated is None:
            self._end_wait(outcome)
            return
        # A failed caller trace callback can interrupt HTTPcore's release. Do
        # not invent protocol completion merely because the caller lease ended.
        self._record_finished()

    def peer_settings(
        self,
        state: PeerLimitState,
        limit: int | None,
        available: int | None,
        *,
        closed: bool,
        changed: bool,
        previous: int | None,
        version: HTTPVersion | None,
    ) -> None:
        if not self._span.is_recording():
            return
        attributes: dict[str, str | int | bool] = {
            "http2.peer.stream_limit.state": state,
            "http2.peer.connection_closed": closed,
            "network.protocol.version": "2"
            if version == "HTTP/2"
            else "1.1"
            if version == "HTTP/1.1"
            else "unknown",
        }
        if limit is not None:
            attributes["http2.peer.max_concurrent_streams"] = limit
        if available is not None:
            attributes["http2.peer.streams.available"] = available
        for name, value in attributes.items():
            self._span.set_attribute(name, value)
        if changed:
            if previous is not None:
                attributes["http2.peer.previous_max_concurrent_streams"] = previous
            self._span.add_event("http2.peer_settings", attributes)

    def _record_finished(self) -> None:
        if self._recorded or not self._finished or self._ended is None:
            return
        self._recorded = True
        assert self._outcome is not None
        attributes = self._attributes | {"outcome": self._outcome}
        self._instruments.closed.add(1, attributes)
        assert self._allocated is not None and self._ended is not None
        self._instruments.duration.record(self._ended - self._allocated, attributes)
        self._instruments.request_body.record(self._request_bytes, attributes)
        self._instruments.response_body.record(self._response_bytes, attributes)

    def _end_wait(self, outcome: Literal["allocated"] | StreamOutcome) -> None:
        if not self._pending:
            return
        self._pending = False
        self._instruments.pending.add(-1, self._attributes)
        self._instruments.waiting.record(
            time.perf_counter() - self._admitted,
            self._attributes | {"outcome": outcome},
        )
