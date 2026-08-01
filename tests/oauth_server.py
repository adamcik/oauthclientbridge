import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Self, cast

from oauthclientbridge import types


@dataclass(frozen=True)
class ObservedRequest:
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes
    client_address: tuple[str, int]


@dataclass
class _Expectation:
    method: str
    path: str
    status: HTTPStatus
    headers: Mapping[str, str]
    body: bytes


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 # Required by BaseHTTPRequestHandler.
        oauth_server = cast("_OAuthServer", self.server).oauth_server
        expectation = oauth_server._next_expectation(
            ObservedRequest(
                method=self.command,
                path=self.path,
                headers=dict(self.headers),
                body=self.rfile.read(int(self.headers["Content-Length"])),
                client_address=self.client_address,
            )
        )
        self.send_response(expectation.status)
        for name, value in expectation.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(expectation.body)))
        self.end_headers()
        self.wfile.write(expectation.body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


class _OAuthServer(ThreadingHTTPServer):
    oauth_server: "OAuthServer"


class Expectation:
    def __init__(self, oauth_server: "OAuthServer", path: str) -> None:
        self._oauth_server = oauth_server
        self._path = path

    def respond(
        self,
        response: types.JsonDict | None = None,
        *,
        status: int = HTTPStatus.OK,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        body = b"" if response is None else json.dumps(response).encode()
        response_headers = (
            {} if response is None else {"Content-Type": "application/json"}
        )
        if headers is not None:
            response_headers.update(headers)
        self._oauth_server._expectations.append(
            _Expectation("POST", self._path, HTTPStatus(status), response_headers, body)
        )


class OAuthServer:
    def __init__(self) -> None:
        self._expectations: deque[_Expectation] = deque()
        self._lock = Lock()
        self.requests: list[ObservedRequest] = []
        self._server = _OAuthServer(("127.0.0.1", 0), _RequestHandler)
        self._server.oauth_server = self
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def expect(self, path: str) -> Expectation:
        return Expectation(self, path)

    def url_for(self, path: str) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}{path}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def _next_expectation(self, request: ObservedRequest) -> _Expectation:
        with self._lock:
            self.requests.append(request)
            if not self._expectations:
                raise AssertionError("HTTP server received an unexpected request")
            expectation = self._expectations.popleft()

        if request.method != expectation.method or request.path != expectation.path:
            raise AssertionError(
                f"Expected {expectation.method} {expectation.path}, "
                f"received {request.method} {request.path}"
            )
        return expectation

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
