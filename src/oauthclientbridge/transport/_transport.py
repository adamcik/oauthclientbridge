import asyncio
import logging
import math
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Self

import httpx
from opentelemetry import metrics, trace

from . import _connection, _observability
from ._models import (
    Clock,
    ConnectionState,
    EventKind,
    HTTPVersion,
    Reason,
    TransportEvent,
    TransportState,
)

logger = logging.getLogger(__name__)


class TransportNotReady(httpx.TransportError):
    """Admission failed before the operation was sent."""


class PreparedHTTPTransport(httpx.AsyncBaseTransport):
    """Single-origin, asyncio HTTP transport with replaceable connections.

    One connection admits operations; a second may be preparing or draining.
    Replacement affects only new admissions. Existing responses retain their
    leases until closure or failure, even after forced socket closure.
    max_streams limits local H2 leases; H1 admits one operation per connection.

    versions lists TLS negotiation preferences, not request retry attempts.
    The default is H2-only. Include HTTP/1.1 to allow H1, including peers
    without ALPN. Preparation verifies TLS without sending an HTTP request;
    H2 initialization starts on first work. Local saturation fails immediately.
    Operations are never retried and proxy connections are unsupported.

    Entering a directly owning AsyncClient calls start(). Call it explicitly
    before using an adapter that does not forward context entry. High-level
    HTTPX tracing requires explicit custom-transport instrumentation.

    A supplied SSL context belongs exclusively to this transport, which sets
    its ALPN offers from versions. Observers run on the owning event loop and must
    not block. Their exceptions are logged without their contents and do not
    affect transport progress.

    Configure the OTel SDK before start(), or supply providers explicitly.
    """

    def __init__(
        self,
        origin: str,
        *,
        versions: tuple[HTTPVersion, ...] = ("HTTP/2",),
        ssl_context: ssl.SSLContext | None = None,
        max_admission_age: float = 60,
        prepare_ahead: float = 10,
        health_interval: float = 5,
        retry_interval: float = 2,
        drain_timeout: float = 10,
        connect_timeout: float = 3,
        max_streams: int = 4,
        clock: Clock | None = None,
        observer: Callable[[TransportEvent], None] | None = None,
        tracer_provider: trace.TracerProvider | None = None,
        meter_provider: metrics.MeterProvider | None = None,
    ) -> None:
        self._origin = httpx.URL(origin)
        if self._origin.scheme != "https" or self._origin.userinfo:
            raise ValueError("one credential-free HTTPS origin required")
        alpn = _connection.alpn_protocols(versions)
        self._versions = versions
        self._context = ssl_context or ssl.create_default_context()
        if (
            not self._context.check_hostname
            or self._context.verify_mode != ssl.CERT_REQUIRED
        ):
            raise ValueError("certificate and hostname verification are required")
        intervals = (
            max_admission_age,
            prepare_ahead,
            health_interval,
            retry_interval,
            drain_timeout,
            connect_timeout,
        )
        if not all(math.isfinite(value) and value > 0 for value in intervals):
            raise ValueError("transport intervals must be finite and positive")
        if prepare_ahead >= max_admission_age or max_streams < 1:
            raise ValueError(
                "prepare_ahead must precede the age limit; max_streams must be positive"
            )
        self._context.set_alpn_protocols(alpn)
        self._max_age, self._ahead = max_admission_age, prepare_ahead
        self._health_interval, self._retry_interval = health_interval, retry_interval
        self._drain_timeout, self._connect_timeout = drain_timeout, connect_timeout
        self._max_streams = max_streams
        self._clock = clock or _Clock()
        self._observer = observer
        self._changed = asyncio.Event()
        self._revision = asyncio.Event()
        self._slots: dict[int, _Slot] = {}
        self._leases: dict[int, _Lease] = {}
        self._current: _Slot | None = None
        self._next_connection = self._next_lease = 0
        self._preparing = self._closing = False
        self._build_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._builder: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._diagnostics = _observability.Diagnostics(
            self._origin.host,
            self._origin.port or 443,
            self._max_age,
            tracer_provider,
            meter_provider,
        )

    async def start(self) -> None:
        """Initialize once; repeated calls share the initial result."""
        asyncio.get_running_loop()
        if self._closing:
            raise RuntimeError("transport is closing")
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._initialize())
        await self._start_task

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (request.url.scheme, request.url.raw_host, request.url.port or 443) != (
            "https",
            self._origin.raw_host,
            self._origin.port or 443,
        ):
            self._diagnostics.admission("origin_mismatch")
            raise TransportNotReady(
                "transport is bound to one HTTPS origin", request=request
            )
        slot = self._current
        if self._closing or slot is None:
            self._diagnostics.admission("unavailable")
            raise TransportNotReady("no prepared connection", request=request)
        if self._clock.now() - slot.born >= self._max_age:
            self._diagnostics.admission("expired")
            self._retire(slot, "age_limit")
            raise TransportNotReady(
                "connection admission age exceeded", request=request
            )
        if slot.leases >= slot.wire.admission_limit(self._max_streams):
            self._diagnostics.admission("saturated")
            raise TransportNotReady(
                "connection admission capacity exhausted", request=request
            )
        if not slot.wire.available():
            self._diagnostics.admission("unhealthy")
            self._retire(slot, "peer_closure")
            raise TransportNotReady(
                "connection is no longer available", request=request
            )
        self._next_lease += 1
        span = self._diagnostics.lease(slot.id, self._next_lease)
        started = time.perf_counter()
        lease = _Lease(
            id=self._next_lease,
            slot=slot,
            started=started,
            span=span,
            measurements=slot.measurements.operation(span, started),
        )
        slot.leases += 1
        self._leases[lease.id] = lease
        self._diagnostics.admission("admitted")
        self._emit("acquired", slot)
        try:
            response = await slot.wire.handle(request, lease.measurements)
        except BaseException as exc:
            self._failed(lease, exc)
            self._retire(slot, "operation_failure")
            await self._release(lease)
            raise
        response.extensions["prepared_connection_id"] = slot.id
        response.extensions["prepared_readiness"] = slot.wire.readiness
        body = response.stream
        assert isinstance(body, httpx.AsyncByteStream)

        async def complete(error: BaseException | None, eof: bool) -> None:
            await self._finish_response(body, lease, error, eof)

        response.stream = _LeaseBody(body, complete)
        return response

    async def cycle(self) -> None:
        """Prepare and publish a replacement without retiring the current one on failure."""
        if self._start_task is None:
            raise RuntimeError("transport has not been started")
        await self._start_task
        await self._cycle("planned")

    def discard(self, connection_id: int) -> None:
        """Stop new admissions on this transport's connection ID; retain its leases."""
        slot = self._slots.get(connection_id)
        if slot is not None:
            self._retire(slot, "problem")

    def snapshot(self) -> TransportState:
        now = self._clock.now()
        current = self._current
        capacity = (
            max(0, current.wire.admission_limit(self._max_streams) - current.leases)
            if current is not None
            and not self._closing
            and current.wire.available()
            and now - current.born < self._max_age
            else 0
        )
        return TransportState(
            current_connection_id=current.id if current else None,
            connections=tuple(
                ConnectionState(
                    id=slot.id,
                    leases=slot.leases,
                    retiring=slot.retired_at is not None,
                    admission_age=now - slot.born,
                    version=slot.wire.version,
                )
                for slot in self._slots.copy().values()
            ),
            preparing=self._preparing,
            closing=self._closing,
            active_leases=len(self._leases),
            available_streams=capacity,
        )

    async def aclose(self) -> None:
        """Wait up to drain_timeout for leases, then close remaining sockets.

        Network cleanup has a separate I/O timeout. Concurrent calls share one
        cleanup task; cancelling a waiter does not cancel that task.
        """
        asyncio.get_running_loop()
        if self._close_task is None:
            self._closing = True
            self._wake()
            self._close_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._close_task)

    async def _cycle(self, reason: Reason) -> None:
        async with self._build_lock:
            if self._closing or len(self._slots) >= 2:
                raise TransportNotReady(
                    "preparation unavailable: shutdown or two-socket capacity"
                )
            self._builder = asyncio.create_task(self._prepare_and_publish(reason))
            try:
                await self._builder
            finally:
                self._builder = None
                self._wake()

    async def _initialize(self) -> None:
        await self._cycle("startup")
        self._worker = asyncio.create_task(
            self._maintain(), name="prepared-http-maintenance"
        )

    async def _maintain(self) -> None:
        while True:
            self._changed.clear()
            if self._builder is not None:
                await self._wait(self._clock.now() + self._health_interval)
                continue
            try:
                for slot in self._slots.copy().values():
                    if slot.retired_at is not None:
                        expired = (
                            self._clock.now() >= slot.retired_at + self._drain_timeout
                        )
                        if not slot.leases or expired:
                            await self._close_slot(slot, forced=bool(slot.leases))
                slot = self._current
                if slot is not None and self._clock.now() - slot.born >= self._max_age:
                    self._retire(slot, "age_limit")
                elif slot is not None and not slot.wire.available():
                    self._retire(slot, "peer_closure")
                slot = self._current
                if slot is None:
                    await self._cycle("refill")
                elif (
                    self._clock.now() >= slot.born + self._max_age - self._ahead
                    and len(self._slots) < 2
                ):
                    await self._cycle("scheduled")
            except Exception as exc:  # noqa: BLE001 - Keep owned maintenance alive, never retry HTTP operations.
                # Retry owned preparation/cleanup, never the HTTP operation.
                logger.warning("HTTP maintenance failed (%s)", type(exc).__name__)
                self._emit("maintenance_failure")
                self._changed.clear()
                await self._wait(self._clock.now() + self._retry_interval)
                continue
            deadline = self._clock.now() + self._health_interval
            if self._current is not None and len(self._slots) < 2:
                deadline = min(
                    deadline, self._current.born + self._max_age - self._ahead
                )
            for slot in self._slots.values():
                if slot.retired_at is not None:
                    deadline = min(deadline, slot.retired_at + self._drain_timeout)
            await self._wait(deadline)

    async def _wait(self, deadline: float) -> None:
        changed = asyncio.create_task(self._changed.wait())
        timer = asyncio.create_task(self._clock.sleep_until(deadline))
        try:
            await asyncio.wait((changed, timer), return_when=asyncio.FIRST_COMPLETED)
        finally:
            changed.cancel()
            timer.cancel()
            await asyncio.gather(changed, timer, return_exceptions=True)

    async def _prepare_and_publish(self, reason: Reason) -> None:
        self._next_connection += 1
        measurements = self._diagnostics.connection()
        slot = _Slot(
            id=self._next_connection,
            wire=_connection.Connection(
                self._origin,
                self._context,
                self._connect_timeout,
                self._wake,
                measurements,
                self._versions,
            ),
            measurements=measurements,
            born=self._clock.now(),
        )
        self._preparing = True
        try:
            self._emit("prepare_started", slot, reason=reason)
            try:
                with self._diagnostics.preparing(slot.id, reason) as span:
                    await slot.wire.prepare(self._diagnostics.phase)
                    span.set_attribute("readiness", slot.wire.readiness)
                    if slot.wire.version is not None:
                        span.set_attribute(
                            "network.protocol.version",
                            "2" if slot.wire.version == "HTTP/2" else "1.1",
                        )
                    if self._closing or not slot.wire.available():
                        raise TransportNotReady(
                            "candidate unavailable before publication"
                        )
                    if self._clock.now() - slot.born >= self._max_age:
                        raise TransportNotReady("candidate exceeded its admission age")
            except BaseException:
                try:
                    await slot.wire.aclose()
                finally:
                    self._emit("prepare_failed", slot, reason=reason)
                raise
            previous = self._current
            self._slots[slot.id] = slot
            self._current = slot
            self._preparing = False
            if previous is not None:
                self._retire(previous, "planned")
            self._emit("published", slot, reason=reason)
            # Publication has succeeded: old cleanup failures must not be
            # reported as preparation failures or roll back the successor.
            if previous is not None and not previous.leases:
                await self._close_slot(previous)
        finally:
            self._preparing = False

    def _retire(self, slot: "_Slot", reason: Reason) -> None:
        if slot.retired_at is not None:
            return
        slot.retired_at, slot.reason = self._clock.now(), reason
        for lease in self._leases.copy().values():
            if lease.slot is slot:
                lease.span.add_event("connection.retired", {"reason": reason})
        if self._current is slot:
            self._current = None
        self._emit("retired", slot)

    def _failed(self, lease: "_Lease", exc: BaseException) -> None:
        if lease.failure is None:
            lease.failure = (
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            )
            self._diagnostics.failed(lease.span, exc, lease.failure)

    async def _finish_response(
        self,
        body: httpx.AsyncByteStream,
        lease: "_Lease",
        error: BaseException | None,
        eof: bool,
    ) -> None:
        lease.eof = eof
        if error is not None:
            self._failed(lease, error)
            self._retire(lease.slot, "operation_failure")
        elif not eof:
            # HTTPcore does not promise RST_STREAM on early response closure.
            self._retire(lease.slot, "response_abandoned")
        try:
            await body.aclose()
        except BaseException as exc:
            self._failed(lease, exc)
            self._retire(lease.slot, "operation_failure")
            raise
        finally:
            await self._release(lease)

    async def _release(self, lease: "_Lease") -> None:
        if self._leases.pop(lease.id, None) is None:
            return
        slot = lease.slot
        slot.leases -= 1
        self._diagnostics.released(
            lease.span,
            lease.started,
            lease.failure or ("complete" if lease.eof else "abandoned"),
        )
        lease.measurements.finish(
            lease.failure or ("complete" if lease.eof else "abandoned")
        )
        self._emit("released", slot)
        if slot.retired_at is not None and not slot.leases:
            await self._close_slot(slot)
        elif not slot.wire.available():
            self._retire(slot, "peer_closure")
            if not slot.leases:
                await self._close_slot(slot)

    async def _close_slot(self, slot: "_Slot", *, forced: bool = False) -> None:
        async with slot.lock:
            if slot.id not in self._slots:
                return
            if forced:
                for lease in self._leases.copy().values():
                    if lease.slot is slot:
                        lease.span.add_event("connection.force_closed")
            try:
                await slot.wire.aclose()
            except BaseException:
                self._emit("close_failed", slot)
                raise
            del self._slots[slot.id]
            if slot.retired_at is not None:
                self._diagnostics.drained(
                    self._clock.now() - slot.retired_at, slot.reason, forced
                )
            self._emit("forced_closed" if forced else "closed", slot)

    async def _shutdown(self) -> None:
        tasks = {
            task
            for task in (self._worker, self._builder, self._start_task)
            if task is not None
        }
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for slot in self._slots.copy().values():
            self._retire(slot, "shutdown")
        try:
            async with asyncio.timeout(self._drain_timeout):
                while True:
                    revision = self._revision
                    if not any(slot.leases for slot in self._slots.values()):
                        break
                    await revision.wait()
        except TimeoutError:
            pass
        errors: list[Exception] = []
        for slot in self._slots.copy().values():
            try:
                await self._close_slot(slot, forced=bool(slot.leases))
            except Exception as exc:  # noqa: BLE001 - Attempt every resource before reporting cleanup failures.
                errors.append(exc)
        if errors:
            raise ExceptionGroup("HTTP transport cleanup failed", errors)

    def _wake(self) -> None:
        self._diagnostics.observe(self.snapshot())
        revision, self._revision = self._revision, asyncio.Event()
        revision.set()
        self._changed.set()

    def _emit(
        self, kind: EventKind, slot: "_Slot | None" = None, *, reason: Reason = ""
    ) -> None:
        self._wake()
        self._diagnostics.event(kind, reason or (slot.reason if slot else ""))
        if self._observer is not None:
            try:
                self._observer(
                    TransportEvent(
                        kind=kind,
                        connection_id=slot.id if slot else None,
                        reason=reason or (slot.reason if slot else ""),
                        state=self.snapshot(),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - Observation must not alter transport behavior.
                logger.warning("Transport observer failed (%s)", type(exc).__name__)


class _Clock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep_until(self, deadline: float) -> None:
        await asyncio.sleep(max(0, deadline - self.now()))


@dataclass
class _Slot:
    id: int
    wire: _connection.Connection
    measurements: _observability.ConnectionMeasurements
    born: float
    leases: int = 0
    retired_at: float | None = None
    reason: Reason = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _Lease:
    id: int
    slot: _Slot
    started: float
    span: trace.Span
    measurements: _observability.StreamMeasurements
    eof: bool = False
    failure: Literal["failed", "cancelled"] | None = None


class _LeaseBody(httpx.AsyncByteStream):
    def __init__(
        self,
        body: httpx.AsyncByteStream,
        complete: Callable[[BaseException | None, bool], Awaitable[None]],
    ) -> None:
        self._body, self._complete = body, complete
        self._closed = self._eof = False
        self._error: BaseException | None = None

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for data in self._body:
                yield data
            self._eof = True
        except BaseException as exc:
            self._error = exc
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._complete(self._error, self._eof)
