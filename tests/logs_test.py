import json
import logging
import sys

import pytest
import structlog
from flask import Flask

from oauthclientbridge import logs


@pytest.fixture(autouse=True)
def reset_logging_handlers():
    """Fixture to reset logging handlers after each test."""
    yield

    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    structlog.reset_defaults()


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
        logs.logger.info("Inside Flask app route.")
        return "Hello, World!"

    with app.test_client() as client:
        response = client.get("/")
        assert response.status_code == 200
        assert b"Hello, World!" in response.data

    captured = capsys.readouterr()
    log_lines = captured.err.strip().splitlines()

    # There should be two log lines: one from inside the route, one from after_request
    assert len(log_lines) == 2

    route_log = json.loads(log_lines[0])
    request_log = json.loads(log_lines[1])

    # Assertions for the log from inside the route
    assert route_log["event"] == "Inside Flask app route."

    # Assertions for the log from after_request
    assert request_log["http"]["status_code"] == 200
    assert request_log["http"]["method"] == "GET"
    assert request_log["http"]["url"].endswith("/")
    assert "duration_ms" in request_log
    assert "response_bytes" in request_log
    assert request_log["response_bytes"] == len(b"Hello, World!")


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
