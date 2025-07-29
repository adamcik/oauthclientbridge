import logging
import sys
import time
from typing import Any

import structlog
from flask import Response, g, request
from opentelemetry import trace
from structlog.types import EventDict

logger: structlog.BoundLogger = structlog.get_logger()


def init(level=logging.INFO, colors: bool = True) -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=False)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
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
        structlog.stdlib.ExtraAdder(),
        add_otel_context_processor,
    ]

    try:
        from structlog_sentry import SentryProcessor

        shared_processors.append(
            SentryProcessor(level=logging.INFO, event_level=logging.ERROR)
        )
    except ImportError:
        pass

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    processors: list[structlog.types.Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
    ]
    if sys.stdout.isatty():
        processors.append(structlog.dev.ConsoleRenderer(colors=colors))
    else:
        processors.extend(
            [
                structlog.processors.dict_tracebacks,
                structlog.processors.JSONRenderer(),
            ]
        )

    formatter = structlog.stdlib.ProcessorFormatter(
        # These run ONLY on `logging` entries from stdlib.
        foreign_pre_chain=shared_processors,
        # These run on ALL entries after the pre_chain is done.
        processors=processors,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    root_logger.setLevel(level)

    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.disabled = True


def add_otel_context_processor(_: Any, __: str, event_dict: EventDict) -> EventDict:
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["otel"] = {
            "trace_id": format(span_context.trace_id, "032x"),
            "span_id": format(span_context.span_id, "016x"),
            "trace_sampled": bool(span_context.trace_flags & trace.TraceFlags.SAMPLED),
        }
    return event_dict


def before_request_log_context():
    structlog.contextvars.clear_contextvars()

    g.start_time = time.perf_counter_ns()


def after_request_log_context(response: Response) -> Response:
    http_version = request.environ.get("SERVER_PROTOCOL")

    logger.info(
        f"""{request.remote_addr} - "{request.method} {request.path} {http_version}" {response.status_code}""",
        duration_ms=(time.perf_counter_ns() - g.start_time) / 1e6,
        response_bytes=len(response.data),
        http={
            "url": str(request.url),
            "status_code": response.status_code,
            "method": request.method,
            "version": http_version,
            "user_agent": request.headers.get("User-Agent"),
            "referer": request.headers.get("Referer"),
        },
        remote_addr=request.remote_addr,
        remote_user=request.remote_user,
    )

    structlog.contextvars.clear_contextvars()
    return response
