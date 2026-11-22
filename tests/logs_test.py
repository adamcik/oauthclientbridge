import asyncio
import json
import logging
import os
import socket
import threading
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

import httpx
import pytest
import structlog
import uvicorn
from opentelemetry import trace
from opentelemetry.semconv.attributes.client_attributes import (
    CLIENT_ADDRESS,
)
from opentelemetry.semconv.attributes.http_attributes import (
    HTTP_REQUEST_HEADER_TEMPLATE,
    HTTP_REQUEST_METHOD,
    HTTP_RESPONSE_HEADER_TEMPLATE,
    HTTP_RESPONSE_STATUS_CODE,
    HTTP_ROUTE,
)
from opentelemetry.semconv.attributes.network_attributes import (
    NETWORK_PROTOCOL_VERSION,
)
from opentelemetry.semconv.attributes.server_attributes import SERVER_ADDRESS
from opentelemetry.semconv.attributes.url_attributes import (
    URL_FULL,
    URL_PATH,
    URL_QUERY,
    URL_SCHEME,
)
from opentelemetry.semconv.attributes.user_agent_attributes import USER_AGENT_ORIGINAL
from pytest_otel_capture import OTelMocker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from oauthclientbridge import asgi, db, logs, startup, types
from oauthclientbridge.settings import LogLevel, LogSettings, Settings

tracer = trace.get_tracer(__name__)

# TODO: Add a test using stdlib logging, with extra=... to make sure that also works.


# TODO: Split this into two tests
def test_configure_structlog_json_output(capsys: pytest.CaptureFixture[str]):
    logs.init_logging(
        LogSettings(
            level=LogLevel.DEBUG,
            colors=False,
            json_output=True,
        )
    )

    # Test standard logging
    std_logger = logging.getLogger("test_std_logger")
    std_logger.info("This is a standard log message.", extra={"std_key": "std_value"})

    # Test structlog
    test_structlog_logger = structlog.get_logger()
    test_structlog_logger.info(
        "This is a structlog message.", struct_key="struct_value"
    )

    captured = capsys.readouterr()

    # Assertions for standard log message (JSON)
    std_log_json = parse_log_record(captured.err.splitlines()[0])
    assert std_log_json["event"] == "This is a standard log message."
    assert std_log_json["level"] == "info"
    assert std_log_json["logger"] == "test_std_logger"
    assert std_log_json["std_key"] == "std_value"

    # Assertions for structlog message (JSON)
    struct_log_json = parse_log_record(captured.err.splitlines()[1])
    assert struct_log_json["event"] == "This is a structlog message."
    assert struct_log_json["level"] == "info"
    assert struct_log_json["logger"] == "tests.logs_test"
    assert struct_log_json["struct_key"] == "struct_value"


def test_reinitializing_logging_does_not_duplicate_server_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Uvicorn can have installed its own non-propagating handler before the
    # factory is called (for example when run through its own CLI).
    server_logger = logging.getLogger("uvicorn.error")
    server_logger.addHandler(logging.StreamHandler())
    server_logger.propagate = False
    server_logger.setLevel(logging.INFO)
    for _ in range(2):
        logs.init_logging(LogSettings(json_output=True, level=LogLevel.WARNING))

    server_logger.info("Suppressed by application log level")
    server_logger.error("Server failed")
    records = parse_logs(capsys)
    assert len(records) == 1
    assert records[0]["logger"] == "uvicorn.error"
    assert records[0]["event"] == "Server failed"


def test_uvicorn_startup_keeps_access_logs_suppressed(
    instrumented: None,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.log = LogSettings(json_output=True)
    settings.database.database = str(tmp_path / "bridge.db")
    db.initialize(settings.database)

    async def requests() -> None:
        ready = asyncio.Event()

        class Server(uvicorn.Server):
            async def startup(self, sockets=None) -> None:
                await super().startup(sockets=sockets)
                ready.set()

        # Match CLI bootstrap -> Uvicorn configuration -> factory bootstrap.
        with startup.bootstrap(settings.log):
            config = uvicorn.Config(
                lambda: asgi.create_app(settings),
                factory=True,
                proxy_headers=False,
                log_config=None,
                access_log=False,
                host="127.0.0.1",
                port=0,
            )
            server = Server(config)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                task = asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    await asyncio.wait_for(ready.wait(), timeout=10)
                    async with httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{listener.getsockname()[1]}"
                    ) as client:
                        for _ in range(2):
                            response = await client.get(
                                "/callback?code=private-code&state=private-state"
                            )
                            assert response.status_code == 400
                            logs.init_logging(settings.log)
                    logging.getLogger("uvicorn.error").error("Server error probe")
                finally:
                    server.should_exit = True
                    await asyncio.wait_for(task, timeout=10)

    asyncio.run(requests())
    output = capsys.readouterr().err
    records = [parse_log_record(line) for line in output.splitlines()]
    access = [r for r in records if r["logger"] == "oauthclientbridge.http"]
    assert len(access) == 2
    assert all(r[URL_PATH] == "/callback" and "?" not in r["event"] for r in access)
    assert not any(r["logger"] == "uvicorn.access" for r in records)
    access_output = "\n".join(
        json.dumps(r)
        for r in records
        if r["logger"] in ("oauthclientbridge.http", "uvicorn.access")
    )
    assert "private-code" not in access_output
    assert "private-state" not in access_output
    server_logs = [r for r in records if r["logger"] == "uvicorn.error"]
    assert sum(r["event"] == "Server error probe" for r in server_logs) == 1
    assert any(r["event"] == "Application startup complete." for r in server_logs)


def test_json_exception_has_frames_but_no_locals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logs.init_logging(LogSettings(json_output=True))
    secret = "frame-local-must-not-be-logged"
    try:
        raise RuntimeError("test failure")
    except RuntimeError:
        logging.getLogger(__name__).exception("Failed")

    output = capsys.readouterr().err
    assert secret not in output
    record = parse_log_record(output)
    exceptions = record["exception"]
    assert isinstance(exceptions, list)
    assert exceptions[0]["exc_type"] == "RuntimeError"
    assert exceptions[0]["frames"]
    assert all("locals" not in frame for frame in exceptions[0]["frames"])


@pytest.fixture
def logging_app(settings: Settings) -> Starlette:
    app = asgi.create_app(settings, initialize_runtime=False)

    async def echo(request: Request) -> Response:
        body = await request.body()
        logging.getLogger(__name__).info("Inside ASGI route.")
        return Response(
            body or b"Hello, World!",
            media_type="text/plain",
            headers={
                "Cache-Control": "no-store",
                "Set-Cookie": "secret-response-cookie",
                "X-Private": "secret-response-header",
            },
        )

    app.router.routes.append(Route("/echo/{name}", echo, methods=["GET", "POST"]))
    return app


@pytest.fixture
async def logging_client(logging_app: Starlette) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=logging_app, client=("127.0.0.1", 12345)),
        base_url="http://bridge.example.com",
    ) as client:
        yield client


@pytest.mark.anyio
async def test_asgi_request_and_response_logging(
    logging_client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    logs.init_logging(LogSettings(json_output=True))
    response = await logging_client.post(
        "/echo/test?param=value",
        content=b"test data",
        headers={"Content-Type": "text/plain", "User-Agent": "test-agent"},
    )
    assert response.status_code == 200
    assert response.content == b"test data"
    records = parse_logs(capsys)
    app_record = next(r for r in records if r["logger"] == "tests.logs_test")
    assert app_record["event"] == "Inside ASGI route."
    access = [r for r in records if r["logger"] == "oauthclientbridge.http"]
    assert len(access) == 1
    record = access[0]
    assert record[CLIENT_ADDRESS] == "127.0.0.1"
    assert record[HTTP_REQUEST_METHOD] == "POST"
    assert record["http.request.body.size"] == len(b"test data")
    assert record[HTTP_RESPONSE_STATUS_CODE] == 200
    assert record["http.response.body.size"] == len(b"test data")
    assert record[NETWORK_PROTOCOL_VERSION] == "1.1"
    assert record[SERVER_ADDRESS] == "bridge.example.com"
    assert record[URL_FULL] == "http://bridge.example.com/echo/test?param=<REDACTED>"
    assert record[URL_PATH] == "/echo/test"
    assert record[URL_QUERY] == "param=<REDACTED>"
    assert record[URL_SCHEME] == "http"
    assert record[USER_AGENT_ORIGINAL] == "test-agent"
    assert record[f"{HTTP_REQUEST_HEADER_TEMPLATE}.content_type"] == "text/plain"
    assert record[f"{HTTP_REQUEST_HEADER_TEMPLATE}.content_length"] == 9
    assert (
        record[f"{HTTP_RESPONSE_HEADER_TEMPLATE}.content_type"]
        == "text/plain; charset=utf-8"
    )
    assert record[f"{HTTP_RESPONSE_HEADER_TEMPLATE}.content_length"] == 9
    assert record[f"{HTTP_RESPONSE_HEADER_TEMPLATE}.cache_control"] == "no-store"
    assert {
        key for key in record if key.startswith(f"{HTTP_RESPONSE_HEADER_TEMPLATE}.")
    } == {
        f"{HTTP_RESPONSE_HEADER_TEMPLATE}.content_type",
        f"{HTTP_RESPONSE_HEADER_TEMPLATE}.content_length",
        f"{HTTP_RESPONSE_HEADER_TEMPLATE}.cache_control",
    }
    assert "secret-response-cookie" not in json.dumps(record)
    assert "secret-response-header" not in json.dumps(record)
    duration = record["http.server.duration"]
    assert isinstance(duration, (int, float)) and duration >= 0
    assert record["event"] == '127.0.0.1 "POST /echo/test 1.1" 200 9 "test-agent"'


@pytest.mark.anyio
async def test_access_log_records_matched_route_template(
    logging_client: httpx.AsyncClient,
    capsys: pytest.CaptureFixture[str],
    otel_mock: OTelMocker,
) -> None:
    logs.init_logging(LogSettings(json_output=True))
    assert (await logging_client.get("/echo/concrete-value")).status_code == 200
    record = next(
        r for r in parse_logs(capsys) if r["logger"] == "oauthclientbridge.http"
    )
    assert record[HTTP_ROUTE] == "/echo/{name}"
    server_span = next(
        span
        for span in otel_mock.get_finished_spans()
        if span.readable_span.kind == trace.SpanKind.SERVER
    )
    assert server_span.attributes is not None
    assert server_span.attributes[HTTP_ROUTE] == "/echo/{name}"


@pytest.mark.anyio
async def test_access_log_redacts_query_and_excludes_sensitive_headers(
    logging_client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    logs.init_logging(LogSettings(json_output=True))
    response = await logging_client.get(
        "/echo/test?code=secret-code&state=secret-state&next=%2Fhome",
        headers={
            "Authorization": "Bearer secret-token",
            "Cookie": "secret-cookie",
            "Referer": "https://secret-referer.example/",
        },
    )
    assert response.status_code == 200
    records = parse_logs(capsys)
    record = next(r for r in records if r["logger"] == "oauthclientbridge.http")
    assert record[URL_FULL] == (
        "http://bridge.example.com/echo/test?code=<REDACTED>&state=<REDACTED>&next=<REDACTED>"
    )
    assert record[URL_QUERY] == "code=<REDACTED>&state=<REDACTED>&next=<REDACTED>"
    assert record[f"{HTTP_REQUEST_HEADER_TEMPLATE}.content_type"] is None
    assert record[f"{HTTP_REQUEST_HEADER_TEMPLATE}.content_length"] is None
    assert record["http.request.body.size"] == 0
    for header in ("authorization", "cookie", "referer"):
        assert f"{HTTP_REQUEST_HEADER_TEMPLATE}.{header}" not in record
    output = json.dumps(record)
    for secret in (
        "secret-code",
        "secret-state",
        "secret-token",
        "secret-cookie",
        "secret-referer",
        "%2Fhome",
    ):
        assert secret not in output


@pytest.mark.anyio
async def test_access_log_bounds_request_attributes(
    logging_client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    logs.init_logging(LogSettings(json_output=True))
    value = "x" * 2048
    response = await logging_client.post(
        f"/echo/{value}",
        headers={"Host": value, "User-Agent": value, "Content-Type": value},
        content=b"",
    )
    assert response.status_code == 200
    record = next(
        r for r in parse_logs(capsys) if r["logger"] == "oauthclientbridge.http"
    )
    assert record[SERVER_ADDRESS] == value[:1024]
    assert record[URL_PATH] == (f"/echo/{value}")[:1024]
    assert record[USER_AGENT_ORIGINAL] == value[:1024]
    assert record[f"{HTTP_REQUEST_HEADER_TEMPLATE}.content_type"] == value[:1024]


@pytest.mark.anyio
async def test_access_log_includes_oauth_error_and_clears_context(
    logging_client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    logs.init_logging(LogSettings(json_output=True))
    response = await logging_client.post("/token", data={"grant_type": "invalid"})
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_grant_type"
    assert (await logging_client.get("/not-found")).status_code == 404
    logging.getLogger(__name__).info("Outside request.")
    records = parse_logs(capsys)
    access = [r for r in records if r["logger"] == "oauthclientbridge.http"]
    assert len(access) == 2
    assert access[0][HTTP_RESPONSE_STATUS_CODE] == 400
    assert access[0]["oauth_error"] == "unsupported_grant_type"
    assert "oauth_error" not in access[1]
    assert "oauth_endpoint" not in access[1]
    outside = next(r for r in records if r["event"] == "Outside request.")
    assert "oauth_error" not in outside
    assert "oauth_endpoint" not in outside


def test_configure_structlog_console_colors(capsys: pytest.CaptureFixture[str]):
    logs.init_logging(
        LogSettings(
            level=LogLevel.DEBUG,
            colors=True,
            json_output=False,
        )
    )

    test_structlog_logger = structlog.get_logger()
    test_structlog_logger.info("This is a colored structlog message.")

    captured = capsys.readouterr()

    # Assertions for colored output (checking for ANSI escape codes)
    # This is a basic check, more robust checks might involve parsing ANSI codes
    assert "\x1b[32m" in captured.err  # Check for green color code (info level)
    assert "This is a colored structlog message." in captured.err


def test_structlog_logging_trace_id_injection(
    instrumented: None, capsys: pytest.CaptureFixture[str]
) -> None:
    logs.init_logging(LogSettings())

    with tracer.start_as_current_span("test") as span:
        structlog.get_logger().info("This is a structlog log entry")

    records = parse_logs(capsys)
    assert len(records) == 1
    assert_has_otel_records(records[0], span)


def test_stdlib_logging_trace_id_injection(
    instrumented: None, capsys: pytest.CaptureFixture[str]
) -> None:
    logs.init_logging(LogSettings())

    with tracer.start_as_current_span("test") as span:
        logging.getLogger(__name__).info("This is a standard log entry")

    records = parse_logs(capsys)
    assert len(records) == 1
    assert_has_otel_records(records[0], span)


def parse_logs(capsys: pytest.CaptureFixture[str]) -> list[types.JsonDict]:
    return [parse_log_record(line) for line in capsys.readouterr().err.splitlines()]


def parse_log_record(line: str) -> types.JsonDict:
    assert line.startswith("{"), repr(line)
    value: object = json.loads(line)
    assert isinstance(value, dict)
    return cast(types.JsonDict, value)


def assert_has_otel_records(record: types.JsonDict, span: trace.Span) -> None:
    assert "trace_id" in record
    assert "span_id" in record
    assert "trace_sampled" in record
    assert record["service.name"] == "tests"
    assert record["service.namespace"] == "oauthclientbridge"
    assert record["service.version"] == "1.2.3"
    assert record["deployment.environment"] == "testing"
    assert record["oauth.provider"] == "spotify"
    assert record["service.instance.id"] == "tests-spotify-testing"
    assert record["vcs.revision"] == "abc1234"
    assert record["process.pid"] == os.getpid()
    assert record["process.thread.id"] == threading.get_ident()
    assert record["process.thread.name"] == threading.current_thread().name

    trace_id = record["trace_id"]
    assert isinstance(trace_id, str)
    assert int(trace_id, 16) == span.get_span_context().trace_id
    span_id = record["span_id"]
    assert isinstance(span_id, str)
    assert int(span_id, 16) == span.get_span_context().span_id


@pytest.mark.anyio
async def test_access_log_formatter(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.log.json_output = True
    settings.log.access_log_format = (
        "{client.address} {http.request.method} {url.path} "
        "{missing.key} {http.request.header.content_type}"
    )
    logs.init_logging(settings.log)
    app = asgi.create_app(settings, initialize_runtime=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge.example.com"
    ) as client:
        assert (await client.get("/not-found")).status_code == 404
    record = next(
        r for r in parse_logs(capsys) if r["logger"] == "oauthclientbridge.http"
    )
    assert record["event"] == "127.0.0.1 GET /not-found {missing.key} -"


@pytest.mark.anyio
@pytest.mark.usefixtures("instrumented")
async def test_access_log_correlates_with_server_span(
    logging_client: httpx.AsyncClient,
    capsys: pytest.CaptureFixture[str],
    otel_mock: OTelMocker,
) -> None:
    logs.init_logging(LogSettings(json_output=True))
    assert (await logging_client.get("/echo/test")).status_code == 200
    records = parse_logs(capsys)
    record = next(r for r in records if r["logger"] == "oauthclientbridge.http")
    server_span = next(
        span
        for span in otel_mock.get_finished_spans()
        if span.readable_span.kind == trace.SpanKind.SERVER
    )
    assert record["trace_id"] == trace.format_trace_id(server_span.trace_id)
    assert record["span_id"] == trace.format_span_id(server_span.span_id)
    app_record = next(r for r in records if r["event"] == "Inside ASGI route.")
    assert app_record["trace_id"] == record["trace_id"]
    assert app_record["span_id"] == record["span_id"]
