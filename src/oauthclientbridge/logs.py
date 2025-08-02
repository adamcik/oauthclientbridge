import logging
import string
import time
from typing import Any, cast

import structlog
from flask import Flask, Request, Response, g, request
from opentelemetry.semconv.attributes.client_attributes import (
    CLIENT_ADDRESS,
    CLIENT_PORT,
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
from structlog.types import EventDict
from werkzeug.datastructures import Headers

from oauthclientbridge.settings import LogSettings

access_logger: structlog.BoundLogger = structlog.get_logger("oauthclientbridge.http")
logger: structlog.BoundLogger = structlog.get_logger()

HTTP_REQUST_DURATION = "http.server.request.duration"
HTTP_REQUEST_BODY_SIZE = "http.server.request.body.size"
HTTP_RESPONSE_BODY_SIZE = "http.server.response.body.size"


class AccessLogFormatter(string.Formatter):
    def get_field(self, field_name, args, kwargs):
        if isinstance(field_name, int):
            return args[field_name]

        if field_name in kwargs:
            return kwargs[field_name], field_name

        data = kwargs
        for part in field_name.split("."):
            if isinstance(data, dict) and part in data:
                data = data[part]
            else:
                return "{" + field_name + "}", field_name

        return data, field_name

    def get_value(self, key, args, kwargs):
        if isinstance(key, int):
            return args[key]
        return kwargs[key] if kwargs[key] is not None else "-"


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


def get_request_info(req: Request, duration_ns: int) -> dict[str, Any]:
    return {
        # "user.name": req.remote_user,  # No direct OTel constant for remote_user
        CLIENT_ADDRESS: req.remote_addr,
        CLIENT_PORT: get_remote_port(req.headers, req.environ),
        HTTP_REQUST_DURATION: duration_ns / 1e9,
        HTTP_REQUEST_METHOD: req.method,
        HTTP_REQUEST_BODY_SIZE: len(req.get_data()),
        HTTP_ROUTE: req.url_rule.rule if req.url_rule else None,
        NETWORK_PROTOCOL_VERSION: req.environ.get("SERVER_PROTOCOL"),
        SERVER_ADDRESS: req.host,
        # TODO: Consider redacting url?
        URL_FULL: str(req.url),
        URL_PATH: req.path,
        # TODO: Consider redacting query?
        URL_QUERY: req.query_string.decode("utf-8"),
        URL_SCHEME: req.scheme,
        USER_AGENT_ORIGINAL: req.headers.get("User-Agent"),
        f"{HTTP_REQUEST_HEADER_TEMPLATE}.content_type": req.content_type,
        f"{HTTP_REQUEST_HEADER_TEMPLATE}.content_length": req.content_length,
        f"{HTTP_REQUEST_HEADER_TEMPLATE}.referer": req.headers.get("Referer"),
    }


def get_response_info(resp: Response) -> dict[str, Any]:
    return {
        HTTP_RESPONSE_BODY_SIZE: len(resp.get_data()),
        HTTP_RESPONSE_STATUS_CODE: resp.status_code,
        f"{HTTP_RESPONSE_HEADER_TEMPLATE}.content_length": resp.content_length,
        f"{HTTP_RESPONSE_HEADER_TEMPLATE}.content_type": resp.content_type,
        f"{HTTP_RESPONSE_HEADER_TEMPLATE}.cache_control": resp.headers.get(
            "Cache-Control"
        ),
    }


def init_access_logs(settings: LogSettings, app: Flask):
    formatter = AccessLogFormatter()

    @app.before_request
    def _before_request_log_context():
        structlog.contextvars.clear_contextvars()
        g.start_time_ns = time.perf_counter_ns()

    @app.after_request
    def _after_request_log_context(response: Response) -> Response:
        data = dict(
            **get_request_info(
                request,
                time.perf_counter_ns() - g.start_time_ns,
            ),
            **get_response_info(response),
        )

        access_logger.info(
            formatter.format(settings.access_log_format, **data),
            **data,
        )

        return response
