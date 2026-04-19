from opentelemetry.sdk.metrics.export import MetricsData
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from oauthclientbridge.compat import (
    InMemoryLogRecordExporter,
    InMemoryMetricReader,
    InMemorySpanExporter,
    SimpleLogRecordProcessor,
    reset_otel_once,
)

from .helpers import CollectedLog, CollectedMetric, CollectedSpan


def _reset_otel_once():
    reset_otel_once()


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
