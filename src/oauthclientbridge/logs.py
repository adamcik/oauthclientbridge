import logging
import sys
import uuid

import structlog
from flask import Response, g, request

logger: structlog.BoundLogger = structlog.get_logger()


def configure_structlog(level=logging.INFO) -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=False)

    shared_processors = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
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

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        ]
        + [
            structlog.dev.ConsoleRenderer(),
        ]
        if sys.stdout.isatty()
        else [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    root_logger.setLevel(level)


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
