from collections.abc import MutableMapping
from typing import Any, NamedTuple

import httpx

class RequestInfo(NamedTuple):
    method: bytes
    url: httpx.URL | tuple[bytes, bytes, int | None, bytes]
    headers: httpx.Headers | None
    stream: httpx.SyncByteStream | httpx.AsyncByteStream | None
    extensions: MutableMapping[str, Any] | None

class ResponseInfo(NamedTuple):
    status_code: int
    headers: httpx.Headers
    stream: httpx.SyncByteStream | httpx.AsyncByteStream | None
    extensions: MutableMapping[str, Any] | None

class HTTPXClientInstrumentor:
    def instrument(self, **kwargs: Any) -> None: ...
    def uninstrument(self, **kwargs: Any) -> None: ...
