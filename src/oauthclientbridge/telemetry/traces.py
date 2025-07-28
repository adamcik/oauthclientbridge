from typing import Any, NoReturn

import structlog

from oauthclientbridge.settings import OtelExporterProtocol, OtelSettings

logger: structlog.BoundLogger = structlog.get_logger()


def _assert_never(value: NoReturn) -> NoReturn:
    raise AssertionError(f"Unhandled type: {value} ({type(value).__name__})")


def init_traces(settings: OtelSettings, span_processor: Any | None = None) -> None:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SpanExporter,
    )

    resource = Resource.create({SERVICE_NAME: settings.service_name})

    provider = TracerProvider(resource=resource)
    if span_processor is None:
        span_exporter: SpanExporter | None
        match settings.exporter_protocol:
            case OtelExporterProtocol.OTLP_GRPC:
                span_exporter = OTLPSpanExporter(endpoint=settings.endpoint)
            case OtelExporterProtocol.CONSOLE:
                span_exporter = ConsoleSpanExporter()
            case None:
                raise ValueError(
                    "exporter_protocol must be set if no span_processor is provided and no exporter protocol is set."
                )
            case _:  # pyright: ignore[reportUnreachable]
                _assert_never(settings.exporter_protocol)
        span_processor = BatchSpanProcessor(span_exporter)
    provider.add_span_processor(span_processor)

    trace.set_tracer_provider(provider)


def shutdown_traces() -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()
