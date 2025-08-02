import json
import logging
import sys

import pytest
import structlog
from flask import Flask
from opentelemetry import trace

from oauthclientbridge import logs

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


def test_configure_structlog_json_output(capsys, monkeypatch):
    # Simulate non-TTY for JSON output
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    logs.init(level=logging.DEBUG, colors=False)

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


def test_flask_request_logging(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    logs.init(level=logging.DEBUG, colors=False)

    app = Flask(__name__)
    app.before_request(logs.before_request_log_context)
    app.after_request(logs.after_request_log_context)

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

    http_record = next(r for r in records if r["logger"] == "oauthclientbridge.http")
    assert http_record["http"]["status_code"] == 200
    assert http_record["http"]["method"] == "GET"
    assert http_record["http"]["url"].endswith("/")
    assert "duration_ms" in http_record
    assert "response_bytes" in http_record
    assert http_record["response_bytes"] == len(b"Hello, World!")


def test_configure_structlog_console_colors(capsys, monkeypatch):
    # Simulate TTY for console output with colors
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    logs.init(level=logging.DEBUG, colors=True)

    test_structlog_logger = structlog.get_logger()
    test_structlog_logger.info("This is a colored structlog message.")

    captured = capsys.readouterr()

    # Assertions for colored output (checking for ANSI escape codes)
    # This is a basic check, more robust checks might involve parsing ANSI codes
    assert "\x1b[32m" in captured.err  # Check for green color code (info level)
    assert "This is a colored structlog message." in captured.err


def test_structlog_logging_trace_id_injection(instrumented, capsys) -> None:
    logs.init()

    with tracer.start_as_current_span("test") as span:
        structlog.get_logger().info("This is a structlog log entry")

    records = parse_logs(capsys)
    assert len(records) == 1
    assert_has_otel_records(records[0], span)


def test_stdlib_logging_trace_id_injection(instrumented, capsys) -> None:
    logs.init()

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
