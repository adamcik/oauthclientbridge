"""Instrumented local HTTP peer; no credentials or external network access."""

import asyncio
import datetime
import ipaddress
import os
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h2.settings
import h11
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass
class Operation:
    connection_id: int
    stream_id: int | None
    path: str
    body: bytes
    headers: dict[str, str]


@dataclass
class Accepted:
    id: int
    socket: socket.socket
    handshake: bool = False
    alpn: str | None = None
    plaintext_bytes: int = 0
    plaintext_sent: int = 0
    writer: asyncio.StreamWriter | None = None
    engine: h2.connection.H2Connection | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    streams: dict[int, dict[str, str]] = field(default_factory=dict)
    bodies: dict[int, bytearray] = field(default_factory=dict)
    gates: dict[int, asyncio.Event] = field(default_factory=dict)
    goaway: bool = False
    ping_acks: int = 0


class _Protocol(asyncio.StreamReaderProtocol):
    def __init__(self, reader: asyncio.StreamReader, accepted: Accepted) -> None:
        super().__init__(reader)
        self.accepted = accepted

    def data_received(self, data: bytes) -> None:
        self.accepted.plaintext_bytes += len(data)
        super().data_received(data)


class HTTP2Peer:
    def __init__(
        self, directory: Path, *, alpn: str | None = "h2", max_streams: int | None = 4
    ) -> None:
        self.context, self.trusted = _contexts(directory, alpn)
        self.max_streams = max_streams
        self.accepted: list[Accepted] = []
        self.operations: list[Operation] = []
        self.headers_received: list[tuple[int, int]] = []
        self.changed = asyncio.Condition()
        self.tls_gate = asyncio.Event()
        self.tls_gate.set()
        self._handlers: list[asyncio.Task[None]] = []

    async def __aenter__(self) -> Self:
        self._listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self._listener.bind(("::1", 0))
        self._listener.listen()
        self._listener.setblocking(False)
        self.origin = f"https://[::1]:{self._listener.getsockname()[1]}"
        self._accept_task = asyncio.create_task(self._accept())
        return self

    async def __aexit__(self, *_: object) -> None:
        self._accept_task.cancel()
        await asyncio.gather(self._accept_task, return_exceptions=True)
        self._listener.close()
        self.tls_gate.set()
        for accepted in self.accepted:
            for gate in accepted.gates.values():
                gate.set()
            if accepted.writer is not None:
                accepted.writer.transport.abort()
        async with asyncio.timeout(5):
            await asyncio.gather(*self._handlers)

    async def until(self, predicate: Callable[[], bool]) -> None:
        async with asyncio.timeout(5):
            async with self.changed:
                await self.changed.wait_for(predicate)

    async def release(self, connection_id: int, stream_id: int) -> None:
        self.accepted[connection_id - 1].gates[stream_id].set()

    async def client_hello(self, connection_id: int) -> None:
        accepted = self.accepted[connection_id - 1]
        descriptor = os.dup(accepted.socket.fileno())
        ready = asyncio.get_running_loop().create_future()

        def readable() -> None:
            if not ready.done():
                ready.set_result(None)

        asyncio.get_running_loop().add_reader(descriptor, readable)
        try:
            async with asyncio.timeout(5):
                await ready
            assert accepted.socket.recv(1, socket.MSG_PEEK) == b"\x16"
        finally:
            asyncio.get_running_loop().remove_reader(descriptor)
            os.close(descriptor)

    async def ping(self, connection_id: int) -> None:
        accepted = self.accepted[connection_id - 1]
        assert accepted.engine is not None and accepted.writer is not None
        async with accepted.lock:
            accepted.engine.ping(b"testping")
            self._write(accepted, accepted.engine.data_to_send())
        await accepted.writer.drain()

    async def goaway(self, connection_id: int) -> None:
        accepted = self.accepted[connection_id - 1]
        assert accepted.engine is not None and accepted.writer is not None
        async with accepted.lock:
            accepted.goaway = True
            accepted.engine.close_connection(
                last_stream_id=max(accepted.streams, default=0)
            )
            self._write(accepted, accepted.engine.data_to_send())
        await accepted.writer.drain()

    async def settings(
        self, connection_id: int, values: dict[h2.settings.SettingCodes, int]
    ) -> None:
        accepted = self.accepted[connection_id - 1]
        assert accepted.engine is not None and accepted.writer is not None
        async with accepted.lock:
            accepted.engine.update_settings(values)
            self._write(accepted, accepted.engine.data_to_send())
        await accepted.writer.drain()

    async def _notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    @staticmethod
    def _write(accepted: Accepted, data: bytes) -> None:
        assert accepted.writer is not None
        accepted.plaintext_sent += len(data)
        accepted.writer.write(data)

    async def _accept(self) -> None:
        while True:
            sock, _ = await asyncio.get_running_loop().sock_accept(self._listener)
            accepted = Accepted(id=len(self.accepted) + 1, socket=sock)
            self.accepted.append(accepted)
            self._handlers.append(asyncio.create_task(self._serve(accepted)))
            await self._notify()

    async def _serve(self, accepted: Accepted) -> None:
        body_tasks: list[asyncio.Task[None]] = []
        try:
            await self.tls_gate.wait()
            reader = asyncio.StreamReader()
            protocol = _Protocol(reader, accepted)
            loop = asyncio.get_running_loop()
            channel, _ = await loop.connect_accepted_socket(
                lambda: protocol,
                accepted.socket,
                ssl=self.context,
                ssl_handshake_timeout=3,
                ssl_shutdown_timeout=1,
            )
            writer = asyncio.StreamWriter(channel, protocol, reader, loop)
            accepted.writer = writer
            accepted.alpn = channel.get_extra_info(
                "ssl_object"
            ).selected_alpn_protocol()
            accepted.handshake = True
            await self._notify()
            if accepted.alpn != "h2":
                await self._http1(accepted, reader)
                return
            engine = h2.connection.H2Connection(
                config=h2.config.H2Configuration(
                    client_side=False, header_encoding="utf-8"
                )
            )
            accepted.engine = engine
            if self.max_streams is None:
                del engine.local_settings[
                    h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS
                ]
            else:
                engine.local_settings = h2.settings.Settings(
                    client=False,
                    initial_values={
                        h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS: self.max_streams
                    },
                )
            engine.initiate_connection()
            self._write(accepted, engine.data_to_send())
            await writer.drain()
            while data := await reader.read(65536):
                lost = False
                async with accepted.lock:
                    for event in engine.receive_data(data):
                        if isinstance(event, h2.events.RequestReceived):
                            accepted.streams[event.stream_id] = dict(event.headers)
                            accepted.bodies[event.stream_id] = bytearray()
                            accepted.gates[event.stream_id] = asyncio.Event()
                            self.headers_received.append((accepted.id, event.stream_id))
                        elif isinstance(event, h2.events.DataReceived):
                            accepted.bodies[event.stream_id].extend(event.data)
                            engine.acknowledge_received_data(
                                event.flow_controlled_length, event.stream_id
                            )
                        elif isinstance(event, h2.events.PingAckReceived):
                            accepted.ping_acks += 1
                        elif isinstance(event, h2.events.StreamEnded):
                            headers = accepted.streams[event.stream_id]
                            path = headers[":path"]
                            self.operations.append(
                                Operation(
                                    connection_id=accepted.id,
                                    stream_id=event.stream_id,
                                    path=path,
                                    body=bytes(accepted.bodies[event.stream_id]),
                                    headers=headers,
                                )
                            )
                            if path == "/lost":
                                channel.abort()
                                lost = True
                                break
                            if path == "/reset":
                                engine.reset_stream(
                                    event.stream_id,
                                    error_code=h2.errors.ErrorCodes.CANCEL,
                                )
                            else:
                                payload = str(accepted.id).encode()
                                engine.send_headers(
                                    event.stream_id,
                                    [
                                        (":status", "200"),
                                        ("content-length", str(len(payload))),
                                    ],
                                )
                                if path == "/hold":
                                    body_tasks.append(
                                        asyncio.create_task(
                                            self._body(
                                                accepted, event.stream_id, payload
                                            )
                                        )
                                    )
                                else:
                                    engine.send_data(
                                        event.stream_id, payload, end_stream=True
                                    )
                    if not lost:
                        self._write(accepted, engine.data_to_send())
                await self._notify()
                if lost:
                    break
                await writer.drain()
        except h2.exceptions.ProtocolError:
            if not accepted.goaway:
                raise
        except (ssl.SSLError, ConnectionError, OSError, h11.RemoteProtocolError):
            pass
        finally:
            for task in body_tasks:
                task.cancel()
            await asyncio.gather(*body_tasks, return_exceptions=True)
            if accepted.writer is not None:
                accepted.writer.close()
                try:
                    await accepted.writer.wait_closed()
                except (ssl.SSLError, ConnectionError, OSError):
                    pass
            else:
                accepted.socket.close()
            accepted.done.set()
            await self._notify()

    async def _body(self, accepted: Accepted, stream_id: int, payload: bytes) -> None:
        await accepted.gates[stream_id].wait()
        assert accepted.engine is not None and accepted.writer is not None
        try:
            async with accepted.lock:
                accepted.engine.send_data(stream_id, payload, end_stream=True)
                self._write(accepted, accepted.engine.data_to_send())
            await accepted.writer.drain()
        except (h2.exceptions.StreamClosedError, ConnectionError):
            pass

    async def _http1(self, accepted: Accepted, reader: asyncio.StreamReader) -> None:
        engine = h11.Connection(h11.SERVER)
        headers: dict[str, str] = {}
        body = bytearray()
        while True:
            event = engine.next_event()
            if event is h11.NEED_DATA:
                data = await reader.read(65536)
                engine.receive_data(data)
            elif isinstance(event, h11.ConnectionClosed):
                return
            elif isinstance(event, h11.Request):
                headers = {
                    key.decode().lower(): value.decode() for key, value in event.headers
                }
                headers[":path"] = event.target.decode()
                headers[":method"] = event.method.decode()
                body = bytearray()
                accepted.gates[0] = asyncio.Event()
                self.headers_received.append((accepted.id, 0))
            elif isinstance(event, h11.Data):
                body.extend(event.data)
            elif isinstance(event, h11.EndOfMessage):
                self.operations.append(
                    Operation(
                        connection_id=accepted.id,
                        stream_id=None,
                        path=headers[":path"],
                        body=bytes(body),
                        headers=headers,
                    )
                )
                await self._notify()
                assert accepted.writer is not None
                if headers[":path"] == "/lost":
                    accepted.writer.transport.abort()
                    return
                payload = str(accepted.id).encode()
                self._write(
                    accepted,
                    engine.send(
                        h11.Response(
                            status_code=200,
                            headers=[(b"Content-Length", str(len(payload)).encode())],
                        )
                    )
                    or b"",
                )
                await accepted.writer.drain()
                if headers[":path"] == "/hold":
                    release = asyncio.create_task(accepted.gates[0].wait())
                    closed = asyncio.create_task(reader.read(65536))
                    try:
                        await asyncio.wait(
                            (release, closed), return_when=asyncio.FIRST_COMPLETED
                        )
                        if closed.done():
                            # This fixture does not accept pipelining. EOF must
                            # terminate a held response without needing release.
                            assert not closed.result(), (
                                "unexpected pipelined H1 request"
                            )
                            return
                    finally:
                        release.cancel()
                        closed.cancel()
                        await asyncio.gather(release, closed, return_exceptions=True)
                self._write(accepted, engine.send(h11.Data(data=payload)) or b"")
                self._write(accepted, engine.send(h11.EndOfMessage()) or b"")
                await accepted.writer.drain()
                if engine.our_state is h11.DONE and engine.their_state is h11.DONE:
                    engine.start_next_cycle()
                else:
                    return


def _contexts(
    directory: Path, alpn: str | None
) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    now = datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local HTTP2 test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HTTP2 local peer")])
        )
        .issuer_name(name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("::1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "server.pem", directory / "server.key"
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.touch(mode=0o600)
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    server.set_alpn_protocols([alpn] if alpn is not None else [])
    trusted = ssl.create_default_context(
        cadata=ca.public_bytes(serialization.Encoding.PEM).decode()
    )
    for context in (server, trusted):
        context.minimum_version = ssl.TLSVersion.TLSv1_3
    return server, trusted
