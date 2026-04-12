import typing
from typing import TypeVar, assert_never

from opentelemetry import trace
from opentelemetry.sdk._logs import LogData
from opentelemetry.sdk.metrics.export import (
    ExponentialHistogramDataPoint,
    HistogramDataPoint,
    Metric,
    NumberDataPoint,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

DataPointType = TypeVar(
    "DataPointType",
    bound=typing.Union[
        NumberDataPoint, HistogramDataPoint, ExponentialHistogramDataPoint
    ],
)


class CollectedSpan:
    def __init__(self, readable_span: ReadableSpan):
        context = readable_span.get_span_context()
        assert context is not None

        self._readable_span: ReadableSpan = readable_span
        self._context: trace.SpanContext = context

    @property
    def name(self):
        return self._readable_span.name

    @property
    def attributes(self):
        return self._readable_span.attributes

    @property
    def resource(self):
        return self._readable_span.resource

    @property
    def scope(self):
        return self._readable_span.instrumentation_scope

    @property
    def span_id(self):
        return self._context.span_id

    @property
    def trace_id(self):
        return self._context.trace_id

    @typing.override
    def __str__(self) -> str:
        return self._readable_span.to_json()


class CollectedLog:
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


class CollectedMetric:
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


def _matches_attributes(
    item_attributes: typing.Mapping[str, typing.Any] | None,
    expected_attributes: typing.Mapping[str, typing.Any] | None,
) -> bool:
    if expected_attributes is None:
        return True
    if item_attributes is None:
        return False
    return all(
        item_attributes.get(key) == value for key, value in expected_attributes.items()
    )


def _matches_scope(
    item_scope: InstrumentationScope | None,
    expected_scope_name: str | None,
) -> bool:
    if expected_scope_name is None:
        return True
    if item_scope is None:
        return False
    return item_scope.name == expected_scope_name


def find_spans(
    spans: list[CollectedSpan],
    name: str,
    attributes: typing.Mapping[str, typing.Any] | None = None,
    scope: str | None = None,
) -> list[CollectedSpan]:
    results: list[CollectedSpan] = []
    for span in spans:
        if (
            span.name == name
            and _matches_attributes(span.attributes, attributes)
            and _matches_scope(span.scope, scope)
        ):
            results.append(span)
    return results


def get_span(
    spans: list[CollectedSpan],
    name: str,
    attributes: typing.Mapping[str, typing.Any] | None = None,
    scope: str | None = None,
) -> CollectedSpan | None:
    results = find_spans(spans, name, attributes, scope)
    if len(results) == 0:
        return None
    if len(results) > 1:
        raise ValueError(
            f"Found more than one span named '{name}' with attributes"
            + f"{attributes} and scope {scope}"
        )
    return results[0]


def find_logs(
    logs: list[CollectedLog],
    attributes: typing.Mapping[str, typing.Any] | None = None,
    scope: str | None = None,
) -> list[CollectedLog]:
    results: list[CollectedLog] = []
    for log in logs:
        if _matches_attributes(log.attributes, attributes) and _matches_scope(
            log.scope, scope
        ):
            results.append(log)
    return results


def get_log(
    logs: list[CollectedLog],
    attributes: typing.Mapping[str, typing.Any] | None = None,
    scope: str | None = None,
) -> CollectedLog | None:
    results = find_logs(logs, attributes, scope)
    if len(results) == 0:
        return None
    if len(results) > 1:
        raise ValueError(
            f"Found more than one log with attributes {attributes} and scope "
            + f"{scope}"
        )
    return results[0]


def find_metrics(
    metrics_data: list[CollectedMetric],
    metric_name: str,
    attributes: typing.Mapping[str, typing.Any] | None = None,
    scope: str | None = None,
) -> list[CollectedMetric]:
    results: list[CollectedMetric] = []
    for collected_metric in metrics_data:
        if collected_metric.name == metric_name and _matches_scope(
            collected_metric.scope, scope
        ):
            # For metrics, attributes are on the data points within the metric
            if attributes is None:
                results.append(collected_metric)
            elif collected_metric.metric.data.data_points:
                # Check if any data point matches the attributes
                if any(
                    _matches_attributes(dp.attributes, attributes)
                    for dp in collected_metric.metric.data.data_points
                ):
                    results.append(collected_metric)
    return results


def get_metric(
    metrics_data: list[CollectedMetric],
    metric_name: str,
    attributes: typing.Mapping[str, typing.Any] | None = None,
    scope: str | None = None,
) -> CollectedMetric | None:
    results = find_metrics(metrics_data, metric_name, attributes, scope)
    if len(results) == 0:
        return None
    if len(results) > 1:
        raise ValueError(
            f"Found more than one metric named '{metric_name}' with attributes "
            + f"{attributes} and scope {scope}"
        )
    return results[0]


def _extract_trace_id(expected: trace.Span | int) -> int:
    match expected:
        case int():
            return expected
        case trace.Span():
            return expected.get_span_context().trace_id
        case _:
            assert_never(expected)


def assert_trace_id(collected_span: CollectedSpan, expected_trace_id: int | trace.Span):
    extepected_trace_id = _extract_trace_id(expected_trace_id)
    assert extepected_trace_id == collected_span.trace_id


def assert_trace_header(header: str, expected_trace: int | trace.Span):
    expected_header = f"00-{_extract_trace_id(expected_trace):032x}-"
    assert header.startswith(expected_header)


def latest_metric_data(
    metrics_data: list[CollectedMetric],
    metric_name: str,
    data_point_type: typing.Type[DataPointType],
    attributes: typing.Mapping[str, typing.Any] | None = None,
    scope: str | None = None,
) -> DataPointType:
    metric = get_metric(metrics_data, metric_name, attributes, scope)
    assert metric is not None
    assert metric.metric.data.data_points is not None
    assert len(metric.metric.data.data_points) > 0
    data_point = metric.metric.data.data_points[-1]
    assert isinstance(data_point, data_point_type)
    return data_point
