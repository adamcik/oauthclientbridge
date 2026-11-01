from starlette.types import ASGIApp, Receive, Scope, Send


class ForwardedHeadersMiddleware:
    """Apply the one trusted Caddy hop on the private Unix-socket boundary."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope["headers"]}
        forwarded_for = _last_header_value(headers.get(b"x-forwarded-for"))
        forwarded_host = _last_header_value(headers.get(b"x-forwarded-host"))
        forwarded_port = _last_header_value(headers.get(b"x-forwarded-port"))
        forwarded_proto = _last_header_value(headers.get(b"x-forwarded-proto"))
        if not any((forwarded_for, forwarded_host, forwarded_port, forwarded_proto)):
            await self.app(scope, receive, send)
            return

        forwarded_scope = dict(scope)
        if forwarded_for is not None:
            forwarded_scope["client"] = (forwarded_for, 0)
        if forwarded_proto is not None:
            forwarded_scope["scheme"] = forwarded_proto
        if forwarded_host is not None:
            host = _host_with_port(forwarded_host, forwarded_port)
            forwarded_scope["headers"] = [
                (key, host.encode("latin-1"))
                if key.lower() == b"host"
                else (key, value)
                for key, value in scope["headers"]
            ]
            forwarded_scope["server"] = (forwarded_host, _port(forwarded_port))

        await self.app(forwarded_scope, receive, send)


def _last_header_value(value: bytes | None) -> str | None:
    if value is None:
        return None
    return value.decode("latin-1").rsplit(",", 1)[-1].strip() or None


def _host_with_port(host: str, port: str | None) -> str:
    if port is None or host.endswith(f":{port}"):
        return host
    return f"{host}:{port}"


def _port(value: str | None) -> int:
    return int(value) if value is not None and value.isdecimal() else 80
