import logging
import sys
import uuid

import structlog
from flask import Response, g, request


def configure_structlog() -> None:
    # TODO: Double check how we want these logs setup.
    processors = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.format_exc_info,
        structlog.processors.CallsiteParameterAdder(
            [
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.LINENO,
                structlog.processors.CallsiteParameter.FUNC_NAME,
            ]
        ),
        structlog.contextvars.merge_contextvars,
    ]

    if sys.stdout.isatty():
        # Development configuration: human-readable output
        processors += [
            structlog.dev.ConsoleRenderer(),
        ]
    else:
        processors += [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.DEBUG,
    )


def before_request_log_context():
    structlog.contextvars.clear_contextvars()

    g.request_id = str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(
        request_id=g.request_id,
        request_info={
            "path": request.path,
            "base_url": request.base_url,
            "method": request.method,
            "remote_address": request.remote_addr,
        },
    )


def after_request_log_context(response: Response) -> Response:
    if hasattr(g, "request_id"):
        response.headers["X-Request-ID"] = g.request_id

    structlog.contextvars.clear_contextvars()
    return response
