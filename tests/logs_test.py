import json
import logging

import pytest
import structlog
from flask import Flask
from opentelemetry import trace
from opentelemetry.semconv.attributes.http_attributes import (
    HTTP_REQUEST_HEADER_TEMPLATE,
    HTTP_REQUEST_METHOD,
    HTTP_RESPONSE_HEADER_TEMPLATE,
    HTTP_RESPONSE_STATUS_CODE,
    HTTP_ROUTE,
)
from opentelemetry.semconv.attributes.server_attributes import SERVER_ADDRESS
from opentelemetry.semconv.attributes.url_attributes import (
    URL_FULL,
    URL_QUERY,
    URL_SCHEME,
)

from oauthclientbridge import logs
from oauthclientbridge.settings import LogLevel, LogSettings

tracer = trace.get_tracer(__name__)


@pytest.fixture(autouse=True)
def reset_logging_handlers():
    """Fixture to reset logging handlers after each test."""
    yield

    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    structlog.reset_defaults()


# TODO: Add a test using stdlib logging, with extra=... to make sure that also works.


# TODO: Split this into two tests
def test_configure_structlog_json_output(capsys):
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
    std_log_json = json.loads(captured.err.splitlines()[0])
    assert std_log_json["event"] == "This is a standard log message."
    assert std_log_json["level"] == "info"
    assert std_log_json["logger"] == "test_std_logger"
    assert std_log_json["std_key"] == "std_value"

    # Assertions for structlog message (JSON)
    struct_log_json = json.loads(captured.err.splitlines()[1])
    assert struct_log_json["event"] == "This is a structlog message."
    assert struct_log_json["level"] == "info"
    assert struct_log_json["logger"] == "tests.logs_test"
    assert struct_log_json["struct_key"] == "struct_value"


def test_flask_request_logging(capsys):
    log_settings = LogSettings(
        level=LogLevel.DEBUG,
        colors=False,
        json_output=True,
    )
    logs.init_logging(log_settings)

    app = Flask(__name__)
    logs.init_access_logs(log_settings, app)

    @app.route("/")
    def index():
        app.logger.info("Inside Flask app route.")
        return "Hello, World!"

    with app.test_client() as client:
        response = client.get("/")
        assert response.status_code == 200
        assert b"Hello, World!" in response.data

    records = parse_logs(capsys)

    app_record = next(r for r in records if r["logger"] == "tests.logs_test")
    assert app_record["event"] == "Inside Flask app route."

    # TODO: Simplify these asserts, we can instead check that the expected to
    # level keys are there and test each of those helpers independently.

    record = next(r for r in records if r["logger"] == "oauthclientbridge.http")
    assert record[HTTP_REQUEST_METHOD] == "GET"
    assert record[HTTP_RESPONSE_STATUS_CODE] == 200
    assert record[HTTP_ROUTE] == "/"
    assert record[SERVER_ADDRESS] == "localhost"
    assert record[URL_FULL].endswith("/")
    assert record[URL_QUERY] == ""
    assert record[URL_SCHEME] == "http"
    assert record[f"{HTTP_REQUEST_HEADER_TEMPLATE}.content_type"] is None
    assert record[f"{HTTP_RESPONSE_HEADER_TEMPLATE}.cache_control"] is None
    assert (
        record[f"{HTTP_RESPONSE_HEADER_TEMPLATE}.content_type"]
        == "text/html; charset=utf-8"
    )
    assert record[logs.HTTP_RESPONSE_BODY_SIZE] == len(b"Hello, World!")
    assert logs.HTTP_REQUST_DURATION in record
    assert record[logs.HTTP_RESPONSE_BODY_SIZE] == len(b"Hello, World!")


def test_configure_structlog_console_colors(capsys):
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


def test_structlog_logging_trace_id_injection(instrumented, capsys) -> None:
    logs.init_logging(LogSettings())

    with tracer.start_as_current_span("test") as span:
        structlog.get_logger().info("This is a structlog log entry")

    records = parse_logs(capsys)
    assert len(records) == 1
    assert_has_otel_records(records[0], span)


def test_stdlib_logging_trace_id_injection(instrumented, capsys) -> None:
    logs.init_logging(LogSettings())

    with tracer.start_as_current_span("test") as span:
        logging.getLogger(__name__).info("This is a standard log entry")

    records = parse_logs(capsys)
    assert len(records) == 1
    assert_has_otel_records(records[0], span)


def parse_logs(capsys):
    return [json.loads(line) for line in capsys.readouterr().err.splitlines()]


def assert_has_otel_records(record, span: trace.Span):
    assert "otelTraceID" in record
    assert "otelSpanID" in record
    assert "otelTraceSampled" in record
    assert "otelServiceName" in record

    assert record["otelTraceID"] == format(span.get_span_context().trace_id, "032x")
    assert record["otelSpanID"] == format(span.get_span_context().span_id, "016x")
