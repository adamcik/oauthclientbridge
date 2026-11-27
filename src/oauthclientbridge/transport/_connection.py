"""Verified HTTP connections with bounded read buffering.

Each connection owns a TLS stream and HTTPcore protocol state. HTTPcore handles
framing and multiplexing; the transport controls admission and retirement.
A bounded reader reports peer closure independently of request reads, without
treating normal TLS or HTTP/2 control traffic as a failure.
"""

import asyncio
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from contextlib import AbstractAsyncContextManager, contextmanager
from typing import cast

import anyio
import httpcore
import httpx

from ._models import HTTPVersion, Phase
from ._observability import ConnectionMeasurements, StreamMeasurements

type PhaseObserver = Callable[[Phase], AbstractAsyncContextManager[None]]
type TraceCallback = Callable[[str, dict[str, object]], Awaitable[None]]

_ALPN: dict[HTTPVersion, str] = {"HTTP/2": "h2", "HTTP/1.1": "http/1.1"}


def alpn_protocols(versions: tuple[HTTPVersion, ...]) -> list[str]:
    if (
        not versions
        or len(set(versions)) != len(versions)
        or any(version not in _ALPN for version in versions)
    ):
        raise ValueError("versions must contain unique supported HTTP versions")
    return [_ALPN[version] for version in versions]


class Connection:
    def __init__(
        self,
        origin: httpx.URL,
        context: ssl.SSLContext,
        timeout: float,
        on_unavailable: Callable[[], None],
        measurements: ConnectionMeasurements,
        versions: tuple[HTTPVersion, ...],
    ) -> None:
        self._origin, self._context = origin, context
        self._timeout, self._on_unavailable = timeout, on_unavailable
        self._measurements = measurements
        self._versions = versions
        self.version: HTTPVersion | None = None
        self.readiness = "tls_protocol"
        self._stream: httpcore.AsyncNetworkStream | None = None
        self._protocol: httpcore.AsyncConnectionInterface | None = None
        self._reader: _BufferedTLSStream | None = None
        self._closed = False
        self._unavailable = asyncio.Event()

    async def prepare(self, phase: PhaseObserver) -> None:
        stream: httpcore.AsyncNetworkStream | None = None
        try:
            if (
                self._context.verify_mode != ssl.CERT_REQUIRED
                or not self._context.check_hostname
            ):
                raise ValueError("certificate and hostname verification are required")
            async with phase("tcp"):
                with _map_errors():
                    # The public export also has a missing-extra placeholder;
                    # our direct httpcore[asyncio] dependency supplies this backend.
                    backend = cast(
                        httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend()
                    )
                    stream = await backend.connect_tcp(
                        self._origin.host,
                        self._origin.port or 443,
                        timeout=self._timeout,
                    )
                self._stream = stream
                self._measurements.connected()

            async with phase("tls"):
                assert stream is not None
                with _map_errors():
                    stream = await stream.start_tls(
                        self._context,
                        server_hostname=self._origin.host,
                        timeout=self._timeout,
                    )
                self._stream = stream
            tls = stream.get_extra_info("ssl_object")
            if not isinstance(tls, (ssl.SSLObject, ssl.SSLSocket)):
                raise httpx.UnsupportedProtocol("TLS protocol negotiation unavailable")
            selected = tls.selected_alpn_protocol()
            for version in self._versions:
                if _ALPN[version] == selected or (
                    selected is None and version == "HTTP/1.1"
                ):
                    self.version = version
                    break
            if self.version is None:
                raise httpx.UnsupportedProtocol(
                    "peer did not negotiate an allowed HTTP version"
                )
            self.readiness = "tls_alpn" if selected is not None else "tls_http1_default"
            self._measurements.negotiated(self.version)
            reader = _BufferedTLSStream(stream, self._peer_closed, self._measurements)
            self._reader = reader
            stream = self._stream = reader
            protocol_type = (
                httpcore.AsyncHTTP2Connection
                if self.version == "HTTP/2"
                else httpcore.AsyncHTTP11Connection
            )
            # The http2 extra supplies the implementation, while its public
            # export also includes a missing-dependency placeholder type.
            self._protocol = cast(
                httpcore.AsyncConnectionInterface,
                protocol_type(
                    httpcore.Origin(
                        b"https", self._origin.raw_host, self._origin.port or 443
                    ),
                    reader,
                ),
            )
        except BaseException:
            # Retain the partial stream if cleanup fails so aclose can retry it.
            if stream is not None:
                await _bounded_close(stream)
                self._measurements.close()
                self._stream = None
            raise

    def available(self) -> bool:
        return (
            not self._closed
            and self._protocol is not None
            and self._reader is not None
            and not self._reader.eof
            and (
                self._protocol.is_available()
                if self.version == "HTTP/2"
                else not self._protocol.is_closed()
            )
            and not self._protocol.has_expired()
        )

    def admission_limit(self, local_limit: int) -> int:
        return local_limit if self.version == "HTTP/2" else 1

    async def handle(
        self, request: httpx.Request, measurements: StreamMeasurements
    ) -> httpx.Response:
        if self._protocol is None or self._closed:
            raise httpx.RemoteProtocolError("prepared connection is closed")
        extensions = dict(cast(dict[str, object], request.extensions))
        previous = cast(TraceCallback | None, extensions.get("trace"))
        allocated = asyncio.Event()

        async def trace(name: str, info: dict[str, object]) -> None:
            if name in (
                "http2.send_request_headers.started",
                "http11.send_request_headers.started",
            ):
                allocated.set()
            measurements.trace(name, info)
            if previous is not None:
                await previous(name, info)

        extensions["trace"] = trace
        assert isinstance(request.stream, httpx.AsyncByteStream)
        request_task = asyncio.create_task(
            self._protocol.handle_async_request(
                httpcore.Request(
                    method=request.method,
                    url=httpcore.URL(
                        scheme=request.url.raw_scheme,
                        host=request.url.raw_host,
                        port=request.url.port,
                        target=request.url.raw_path,
                    ),
                    headers=request.headers.raw,
                    content=_request_body(request.stream, measurements),
                    extensions=extensions,
                )
            )
        )
        assigned = asyncio.create_task(allocated.wait())
        closed = asyncio.create_task(self._unavailable.wait())
        transferred = False
        try:
            with _map_errors():
                await asyncio.wait(
                    (request_task, assigned, closed),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if (
                    not request_task.done()
                    and not allocated.is_set()
                    and self._unavailable.is_set()
                ):
                    # Closing a socket does not wake HTTPcore's stream-credit
                    # semaphore. Cancel only work without an allocated stream;
                    # allocated work must still consume any buffered response.
                    request_task.cancel()
                    await asyncio.gather(request_task, return_exceptions=True)
                    raise httpx.RemoteProtocolError(
                        "connection closed before stream allocation"
                    )
                response = await request_task
            result = httpx.Response(
                response.status,
                headers=response.headers,
                stream=_Body(response, measurements),
                extensions=cast(dict[str, object], response.extensions),
            )
            transferred = True
            return result
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    for task in (request_task, assigned, closed):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(
                        request_task, assigned, closed, return_exceptions=True
                    )
                except BaseException:
                    transferred = False
                    raise
                finally:
                    if (
                        not transferred
                        and request_task.done()
                        and not request_task.cancelled()
                        and request_task.exception() is None
                    ):
                        # Cancellation can race a successful header response,
                        # including while its watcher tasks are being cleaned up.
                        with _map_errors():
                            await request_task.result().aclose()

    def _peer_closed(self) -> None:
        self._unavailable.set()
        self._on_unavailable()

    async def aclose(self) -> None:
        self._closed = True
        self._unavailable.set()
        if self._protocol is not None:
            await _bounded_close(self._protocol)
        elif self._stream is not None:
            await _bounded_close(self._stream)
        self._measurements.close()


async def _bounded_close(
    resource: httpcore.AsyncNetworkStream | httpcore.AsyncConnectionInterface,
) -> None:
    async with asyncio.timeout(2):
        await resource.aclose()


@contextmanager
def _map_errors() -> Generator[None]:
    try:
        yield
    except httpcore.ConnectionNotAvailable as exc:
        raise httpx.RemoteProtocolError(
            "prepared connection no longer accepts work"
        ) from exc
    except (
        httpcore.NetworkError,
        httpcore.ProtocolError,
        httpcore.TimeoutException,
    ) as exc:
        mapping: dict[type[Exception], type[httpx.TransportError]] = {
            httpcore.ConnectTimeout: httpx.ConnectTimeout,
            httpcore.ReadTimeout: httpx.ReadTimeout,
            httpcore.WriteTimeout: httpx.WriteTimeout,
            httpcore.PoolTimeout: httpx.PoolTimeout,
            httpcore.ConnectError: httpx.ConnectError,
            httpcore.ReadError: httpx.ReadError,
            httpcore.WriteError: httpx.WriteError,
            httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
            httpcore.LocalProtocolError: httpx.LocalProtocolError,
        }
        target = mapping.get(type(exc))
        if target is None:
            if isinstance(exc, httpcore.TimeoutException):
                target = httpx.TimeoutException
            elif isinstance(exc, httpcore.ProtocolError):
                target = httpx.ProtocolError
            else:
                target = httpx.NetworkError
        raise target(str(exc)) from exc


class _Body(httpx.AsyncByteStream):
    def __init__(
        self, response: httpcore.Response, measurements: StreamMeasurements
    ) -> None:
        self._response = response
        self._measurements = measurements

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _map_errors():
            async for data in self._response.aiter_stream():
                self._measurements.response_chunk(len(data))
                yield data

    async def aclose(self) -> None:
        with _map_errors():
            await self._response.aclose()


async def _request_body(
    stream: httpx.AsyncByteStream, measurements: StreamMeasurements
) -> AsyncIterator[bytes]:
    async for data in stream:
        measurements.request_chunk(len(data))
        yield data


class _BufferedTLSStream(httpcore.AsyncNetworkStream):
    def __init__(
        self,
        source: httpcore.AsyncNetworkStream,
        on_unavailable: Callable[[], None],
        measurements: ConnectionMeasurements,
    ) -> None:
        self._source, self._on_unavailable = source, on_unavailable
        self._measurements = measurements
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=8)
        self._buffer = b""
        self._error: Exception | None = None
        self.eof = False
        self._reader = asyncio.create_task(
            self._receive(), name="prepared-http-tls-reader"
        )

    async def _receive(self) -> None:
        try:
            while data := await self._source.read(65536):
                self._measurements.transferred("received", len(data))
                await self._queue.put(data)
        except asyncio.CancelledError:
            raise
        except (httpcore.NetworkError, httpcore.ProtocolError, OSError) as exc:
            self._error = exc
        self.eof = True
        self._on_unavailable()
        await self._queue.put(None)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if not self._buffer:
            if self.eof and self._queue.empty():
                return self._end()
            try:
                async with asyncio.timeout(timeout):
                    data = await self._queue.get()
            except TimeoutError as exc:
                raise httpcore.ReadTimeout() from exc
            if data is None:
                return self._end()
            self._buffer = data
        result, self._buffer = self._buffer[:max_bytes], self._buffer[max_bytes:]
        return result

    def _end(self) -> bytes:
        if self._error is not None:
            raise httpcore.ReadError(str(self._error)) from self._error
        return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        try:
            await self._source.write(buffer, timeout)
            self._measurements.transferred("sent", len(buffer))
        except anyio.EndOfStream as exc:
            # A peer can apply a POST and close while a SETTINGS ACK is being
            # written. This is an ambiguous write failure, never a retry signal.
            raise httpcore.WriteError("TLS peer closed during write") from exc

    async def aclose(self) -> None:
        self._reader.cancel()
        try:
            await asyncio.gather(self._reader, return_exceptions=True)
        finally:
            if not self.eof:
                self._error = httpcore.ReadError("prepared TLS stream closed")
            self.eof = True
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            await self._source.aclose()
            self._measurements.close()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise RuntimeError("stream is already TLS-connected")

    def get_extra_info(self, info: str) -> object:
        if info == "is_readable":
            # HTTP/1.1 idle checks must see decoded bytes/EOF, not TLS tickets
            # racing the owned reader on the underlying socket.
            return self.eof or bool(self._buffer) or not self._queue.empty()
        return self._source.get_extra_info(info)
