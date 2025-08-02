import logging
import string
import time
from typing import Any, cast

import structlog
from flask import Flask, Request, Response, g, request
from structlog.types import EventDict
from werkzeug.datastructures import Headers

from oauthclientbridge.settings import LogSettings

access_logger: structlog.BoundLogger = structlog.get_logger("oauthclientbridge.http")
logger: structlog.BoundLogger = structlog.get_logger()


class AccessLogFormatter(string.Formatter):
    def __init__(self):
        self.unknown_keys: set[str] = set()
        super().__init__()

    def get_field(self, field_name, args, kwargs):
        data = kwargs
        for part in field_name.split("."):
            if isinstance(data, dict) and part in data:
                data = data[part]
            else:
                self.unknown_keys.add(field_name)
                return "{" + field_name + "}", field_name
        return data if data is not None else "-", field_name


def init_logging(settings: LogSettings) -> None:
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
        add_otel_context_processor,
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
    ]
    if not settings.json_output:
        processors.append(structlog.dev.ConsoleRenderer(colors=settings.colors))
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

    root_logger.setLevel(settings.level.upper())

    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.disabled = True


def add_otel_context_processor(_: Any, __: str, event_dict: EventDict) -> EventDict:
    if "_record" not in event_dict:
        return event_dict

    record = cast(logging.LogRecord, event_dict["_record"])
    if hasattr(record, "otelTraceID"):
        event_dict["otelTraceID"] = getattr(record, "otelTraceID")
        event_dict["otelSpanID"] = getattr(record, "otelSpanID")
        event_dict["otelTraceSampled"] = getattr(record, "otelTraceSampled")
        if hasattr(record, "otelServiceName"):
            event_dict["otelServiceName"] = getattr(record, "otelServiceName")

    return event_dict


def get_remote_port(headers: Headers, environ: dict[str, Any]) -> str | None:
    return headers.get("X-Forwarded-Port") or environ.get("REMOTE_PORT")


# TODO: Decide on casing to use in the logs events. Otel uses a different style
# than what we have here.


def get_request_info(req: Request) -> dict[str, Any]:
    return {
        "method": req.method,
        "path": req.path,
        "remote_addr": req.remote_addr,
        "remote_port": get_remote_port(req.headers, req.environ),
        "remote_user": req.remote_user,
        "url": str(req.url),
        "user_agent": req.headers.get("User-Agent"),
        "referer": req.headers.get("Referer"),
        "version": req.environ.get("SERVER_PROTOCOL"),
        "scheme": req.scheme,
        "host": req.host,
        "query_string": req.query_string.decode("utf-8"),
        "content_type": req.content_type,
        "content_length": req.content_length,
    }


def get_response_info(resp: Response, duration_ns: int) -> dict[str, Any]:
    return {
        "status_code": resp.status_code,
        "status_name": resp.status,
        "content_length": resp.content_length,
        "duration_ms": duration_ns / 1e6,
        "content_type": resp.content_type,
        "cache_control": resp.headers.get("Cache-Control"),
    }


def get_flask_info(req: Request) -> dict[str, Any]:
    return {
        "endpoint": req.endpoint,
        "args": req.view_args,
        "url_rule": req.url_rule.rule if req.url_rule else None,
        "blueprint": req.blueprint,
    }


def init_access_logs(settings: LogSettings, app: Flask):
    formatter = AccessLogFormatter()
    first_call = True

    @app.before_request
    def _before_request_log_context():
        structlog.contextvars.clear_contextvars()
        g.start_time_ns = time.perf_counter_ns()

    @app.after_request
    def _after_request_log_context(response: Response) -> Response:
        data = {
            "request": get_request_info(request),
            "response": get_response_info(
                response,
                time.perf_counter_ns() - g.start_time_ns,
            ),
            "flask": get_flask_info(request),
        }

        message = formatter.format(settings.access_log_format, **data)
        access_logger.info(message, **data)

        nonlocal first_call
        if first_call:
            first_call = False
            if formatter.unknown_keys:
                logger.warning(
                    "Access log format contains unknown keys",
                    unknown_keys=formatter.unknown_keys,
                )

        return response
