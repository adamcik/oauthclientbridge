import opentelemetry._events
import opentelemetry._logs._internal
import opentelemetry.metrics._internal
import opentelemetry.trace
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricsData
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.util._once import Once

from .helpers import CollectedLog, CollectedMetric, CollectedSpan


def _reset_otel_once():
    # WARNING: This function directly manipulates OpenTelemetry's internal
    # `_Once` objects. OpenTelemetry SDK providers (TracerProvider,
    # MeterProvider, etc.) are designed to be singletons per process, meaning
    # `set_tracer_provider()` can only be called once per Python process.
    #
    # In a testing environment, especially when running tests sequentially
    # within the same process, this singleton behavior prevents re-initializing
    # providers for subsequent tests.
    #
    # This hack resets the internal flags that track whether a provider has
    # been set, allowing `set_tracer_provider()` and similar functions to be
    # called again.
    #
    # Alternatives considered:
    # - Using `pytest-xdist` or `pytest-isolate`: While these provide process
    #   isolation, they don't guarantee a fresh process for every single test
    #   without complex (and sometimes problematic) configuration. If a worker
    #   process is reused, the global OpenTelemetry state persists, leading to
    #   "Overriding" errors.
    #
    # Given the OpenTelemetry SDK's design and the need for reliable test
    # isolation without excessive overhead, directly resetting these internal
    # flags is currently the most pragmatic and least intrusive solution for
    # ensuring a clean OpenTelemetry state before each test.
    opentelemetry.trace._TRACER_PROVIDER_SET_ONCE = Once()
    opentelemetry._logs._internal._LOGGER_PROVIDER_SET_ONCE = Once()
    opentelemetry._events._EVENT_LOGGER_PROVIDER_SET_ONCE = Once()
    opentelemetry.metrics._internal._METER_PROVIDER_SET_ONCE = Once()


class OTelMocker:
    def __init__(
        self,
        log_exporter: InMemoryLogRecordExporter,
        span_exporter: InMemorySpanExporter,
        metric_reader: InMemoryMetricReader,
    ):
        self._log_exporter = log_exporter
        self._span_exporter = span_exporter
        self.metric_reader = metric_reader
        self.span_processor = SimpleSpanProcessor(self._span_exporter)
        self.log_processor = SimpleLogRecordProcessor(self._log_exporter)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        _reset_otel_once()

    def get_finished_logs(self) -> list[CollectedLog]:
        logs: list[CollectedLog] = []
        for log_record in self._log_exporter.get_finished_logs():
            logs.append(CollectedLog(log_record))
        self._log_exporter.clear()
        return logs

    def get_finished_spans(self) -> list[CollectedSpan]:
        spans: list[CollectedSpan] = []
        for span in self._span_exporter.get_finished_spans():
            spans.append(CollectedSpan(span))
        self._span_exporter.clear()
        return spans

    def get_metrics_data(self) -> list[CollectedMetric]:
        metrics: list[CollectedMetric] = []
        data = self.metric_reader.get_metrics_data()
        if isinstance(data, MetricsData):
            for resource_metric in data.resource_metrics:
                resource = resource_metric.resource
                for scope_metrics in resource_metric.scope_metrics:
                    scope = scope_metrics.scope
                    for metric in scope_metrics.metrics:
                        metrics.append(CollectedMetric(resource, scope, metric))

        return metrics
