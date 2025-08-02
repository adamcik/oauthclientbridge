import json
import logging

import pytest
import structlog
from flask import Flask
from opentelemetry import trace

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
    logs.init_logging(
        LogSettings(
            level=LogLevel.DEBUG,
            colors=False,
            json_output=True,
        )
    )

    settings = LogSettings(
        level=LogLevel.DEBUG,
        colors=False,
        json_output=True,
    )
    app = Flask(__name__)
    logs.init_access_logs(app, settings)

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
    http_record = next(r for r in records if r["logger"] == "oauthclientbridge.http")
    assert http_record["response"]["status_code"] == 200
    assert http_record["request"]["method"] == "GET"
    assert http_record["request"]["url"].endswith("/")
    assert "duration_ms" in http_record["response"]
    assert "content_length" in http_record["response"]
    assert http_record["response"]["content_length"] == len(b"Hello, World!")
    assert http_record["request"]["scheme"] == "http"
    assert http_record["request"]["host"] == "localhost"
    assert http_record["request"]["query_string"] == ""
    assert http_record["request"]["content_type"] is None
    # TODO: Fix content_length to be 0 for GET requests
    # assert http_record["request"]["content_length"] == 0
    assert http_record["response"]["status_name"] == "200 OK"
    assert http_record["response"]["content_type"] == "text/html; charset=utf-8"
    assert http_record["response"]["cache_control"] is None
    assert http_record["flask"]["endpoint"] == "index"
    assert http_record["flask"]["args"] == {}
    assert http_record["flask"]["url_rule"] == "/"
    assert http_record["flask"]["blueprint"] is None


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
