import pytest

from oauthclientbridge.observer import RequestLifecycleRequest, RequestLifecycleResponse
from oauthclientbridge.telemetry._lifecycle import RequestLifecycleMiddleware
from oauthclientbridge.types import Endpoint


@pytest.mark.anyio
async def test_asgi_lifecycle_counts_received_chunked_request_bytes() -> None:
    class RecordingObserver:
        def __init__(self) -> None:
            self.request: RequestLifecycleRequest | None = None

        def start(self, request: RequestLifecycleRequest) -> None:
            self.request = request

        def complete(
            self, endpoint: Endpoint, response: RequestLifecycleResponse
        ) -> None:
            pass

    async def app(scope: object, receive: object, send: object) -> None:
        assert callable(receive)
        assert callable(send)
        _ = await receive()
        _ = await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = [
        {"type": "http.request", "body": b"first ", "more_body": True},
        {"type": "http.request", "body": b"second"},
    ]

    async def receive() -> dict[str, object]:
        return messages.pop(0)

    async def send(message: object) -> None:
        pass

    observer = RecordingObserver()
    middleware = RequestLifecycleMiddleware(
        app,
        lifecycle_observer=observer,
        response_header_observer=lambda _: None,
    )
    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/token",
            "headers": [],
            "query_string": b"",
        },
        receive,
        send,
    )

    assert observer.request is not None
    assert observer.request.attributes["http.request.body.size"] == len(b"first second")
