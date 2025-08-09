import typing

import opentelemetry._events
import opentelemetry._logs._internal
import opentelemetry.metrics._internal
import opentelemetry.trace
from opentelemetry.sdk._logs import LogData
from opentelemetry.sdk._logs.export import (
    InMemoryLogExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics._internal.export import InMemoryMetricReader
from opentelemetry.sdk.metrics.export import Metric, MetricsData
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.util._once import Once


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


class LogEntry:
    def __init__(self, log_data: LogData):
        self._log_data: LogData = log_data

    @property
    def scope(self):
        return self._log_data.instrumentation_scope

    @property
    def resource(self):
        return self._log_data.log_record.resource

    @property
    def attributes(self):
        return self._log_data.log_record.attributes

    @property
    def body(self):
        return self._log_data.log_record.body

    @typing.override
    def __str__(self) -> str:
        return self._log_data.log_record.to_json()


class MetricDataPoint:
    def __init__(
        self,
        resource: Resource,
        scope: InstrumentationScope,
        metric: Metric,
    ):
        self._resource: Resource = resource
        self._scope: InstrumentationScope = scope
        self._metric: Metric = metric

    @property
    def resource(self):
        return self._resource

    @property
    def scope(self):
        return self._scope

    @property
    def metric(self):
        return self._metric

    @property
    def name(self):
        return self._metric.name

    @property
    def data(self):
        return self._metric.data

    @typing.override
    def __str__(self) -> str:
        return self._metric.to_json()


class OTelMocker:
    def __init__(self):
        self._finished_spans: list[ReadableSpan] = []
        self._finished_logs: list[LogEntry] = []
        self._metrics_data: list[MetricDataPoint] = []

        self._log_exporter: InMemoryLogExporter = InMemoryLogExporter()
        self._span_exporter: InMemorySpanExporter = InMemorySpanExporter()

        self.metric_reader: InMemoryMetricReader = InMemoryMetricReader()
        self.span_processor: SimpleSpanProcessor = SimpleSpanProcessor(
            self._span_exporter
        )
        self.log_processor: SimpleLogRecordProcessor = SimpleLogRecordProcessor(
            self._log_exporter
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        _reset_otel_once()

    def get_finished_logs(self):
        for log_data in self._log_exporter.get_finished_logs():
            self._finished_logs.append(LogEntry(log_data))
        return self._finished_logs

    def get_finished_spans(self):
        for span in self._span_exporter.get_finished_spans():
            self._finished_spans.append(span)
        return self._finished_spans

    def get_metrics_data(self):
        data = self.metric_reader.get_metrics_data()
        if isinstance(data, MetricsData):
            for resource_metric in data.resource_metrics:
                resource = resource_metric.resource
                for scope_metrics in resource_metric.scope_metrics:
                    scope = scope_metrics.scope
                    for metric in scope_metrics.metrics:
                        self._metrics_data.append(
                            MetricDataPoint(resource, scope, metric)
                        )
        return self._metrics_data

    def get_span_named(self, name: str):
        for span in self.get_finished_spans():
            if span.name == name:
                return span
        return None

    def assert_has_span_named(self, name: str):
        span = self.get_span_named(name)
        finished_spans = [span.name for span in self.get_finished_spans()]
        assert (
            span is not None
        ), f'Could not find span named "{name}"; finished spans: {finished_spans}'

    def assert_does_not_have_span_named(self, name: str):
        span = self.get_span_named(name)
        assert span is None, f"Found unexpected span named {name}"

    def get_event_named(self, name: str):
        for event in self.get_finished_logs():
            if event.attributes is None:
                continue
            if event.attributes.get("event.name") == name:
                return event
        return None

    def get_events_named(self, name: str):
        result: list[LogEntry] = []
        for event in self.get_finished_logs():
            if event.attributes is None:
                continue
            if event.attributes.get("event.name") == name:
                result.append(event)
        return result

    def assert_has_event_named(self, name: str):
        event = self.get_event_named(name)
        finished_logs = self.get_finished_logs()
        assert (
            event is not None
        ), f'Could not find event named "{name}"; finished logs: {finished_logs}'

    def assert_does_not_have_event_named(self, name: str):
        event = self.get_event_named(name)
        assert event is None, f"Unexpected event: {event}"

    def get_metrics_data_named(self, name: str):
        results: list[MetricDataPoint] = []
        for entry in self.get_metrics_data():
            if entry.name == name:
                results.append(entry)
        return results

    def assert_has_metrics_data_named(self, name: str):
        data = self.get_metrics_data_named(name)
        assert len(data) > 0
