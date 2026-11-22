import logging
from typing import Any, cast

import structlog
from structlog.types import EventDict

from oauthclientbridge import telemetry
from oauthclientbridge.settings import LogSettings

logger: structlog.BoundLogger = structlog.get_logger()


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
        processors.append(structlog.processors.format_exc_info)
        processors.append(structlog.dev.ConsoleRenderer(colors=settings.colors))
    else:
        processors.extend(
            [
                structlog.processors.ExceptionRenderer(
                    structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
                ),
                structlog.processors.JSONRenderer(),
            ]
        )

    formatter = structlog.stdlib.ProcessorFormatter(
        # These run ONLY on `logging` entries from stdlib.
        foreign_pre_chain=shared_processors,
        # These run on ALL entries after the pre_chain is done.
        processors=processors,
    )

    handler = _ApplicationLogHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    for existing in root_logger.handlers[:]:
        if isinstance(existing, _ApplicationLogHandler):
            root_logger.removeHandler(existing)
            existing.close()
    root_logger.addHandler(handler)

    root_logger.setLevel(settings.level)

    for name, propagate in {
        "uvicorn": True,
        "uvicorn.error": True,
        # Use our own enriched access logs without duplicating them in Uvicorn.
        "uvicorn.access": False,
    }.items():
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = propagate
        server_logger.setLevel(logging.NOTSET)


class _ApplicationLogHandler(logging.StreamHandler[Any]):
    """An owned sink that can be replaced without removing host/test handlers."""


def add_otel_context_processor(_: Any, __: str, event_dict: EventDict) -> EventDict:
    if "_record" in event_dict:
        record = cast(logging.LogRecord, event_dict["_record"])
        if hasattr(record, "telemetry_attributes"):
            event_dict.update(getattr(record, "telemetry_attributes"))  # noqa: B009 - Added dynamically by the logging hook.
            return event_dict

    event_dict.update(telemetry.otel_log_attributes())

    return event_dict
