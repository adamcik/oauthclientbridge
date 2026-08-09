import anyio
import pytest
from opentelemetry.sdk.metrics.export import HistogramDataPoint, NumberDataPoint

from oauthclientbridge.telemetry import _asyncio

from .plugins import otel


def test_asyncio_monitor_reports_named_loop_health(
    otel_mock: otel.OTelMocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ScheduledHandle:
        def __init__(self, *, cancelled: bool) -> None:
            self._cancelled = cancelled

        def cancelled(self) -> bool:
            return self._cancelled

    class Loop:
        _ready = (object(),)
        _scheduled = (
            ScheduledHandle(cancelled=False),
            ScheduledHandle(cancelled=True),
        )

    monitor = _asyncio.AsyncioMonitor("main")
    monitor._loop = Loop()  # type: ignore[assignment] # Controlled loop internals.
    monkeypatch.setattr(_asyncio.asyncio, "all_tasks", lambda _loop: set())

    monitor._snapshot()  # pyright: ignore[reportPrivateUsage] # Implementation test.
    monitor._record_tick(0.25)  # pyright: ignore[reportPrivateUsage] # Implementation test.

    metrics = otel_mock.get_metrics_data()
    attributes = {"asyncio.loop.name": "main"}
    delay = otel.latest_metric_data(
        metrics,
        "asyncio.event_loop.schedule_delay",
        HistogramDataPoint,
        attributes=attributes,
        scope="oauthclientbridge.telemetry._asyncio",
    )
    heartbeat = otel.latest_metric_data(
        metrics,
        "asyncio.event_loop.heartbeat",
        NumberDataPoint,
        attributes=attributes,
        scope="oauthclientbridge.telemetry._asyncio",
    )
    ready = otel.latest_metric_data(
        metrics,
        "asyncio.event_loop.ready_callbacks",
        NumberDataPoint,
        attributes=attributes,
        scope="oauthclientbridge.telemetry._asyncio",
    )
    scheduled = otel.latest_metric_data(
        metrics,
        "asyncio.event_loop.scheduled_callbacks",
        NumberDataPoint,
        attributes=attributes,
        scope="oauthclientbridge.telemetry._asyncio",
    )

    assert delay.sum == 0.25
    assert delay.count == 1
    assert heartbeat.value == 1
    assert ready.value >= 1
    assert scheduled.value >= 1


@pytest.mark.anyio
async def test_asyncio_monitor_disables_unknown_loop_internals() -> None:
    monitor = _asyncio.AsyncioMonitor("main")
    monitor._loop = object()  # type: ignore[assignment] # Compatibility test.

    monitor._snapshot()  # pyright: ignore[reportPrivateUsage] # Implementation test.

    assert monitor._introspection_enabled is False  # pyright: ignore[reportPrivateUsage] # Implementation test.
    assert tuple(monitor._observe_ready_callbacks(None)) == ()  # pyright: ignore[reportPrivateUsage] # Implementation test.


@pytest.mark.anyio
async def test_asyncio_monitor_stops_observing_after_cancellation() -> None:
    monitor = _asyncio.AsyncioMonitor("main")

    async with anyio.create_task_group() as group:
        group.start_soon(monitor.run)
        await anyio.sleep(0)
        group.cancel_scope.cancel()

    assert tuple(monitor._observe_ready_callbacks(None)) == ()  # pyright: ignore[reportPrivateUsage] # Lifecycle test.


def test_asyncio_monitor_replaces_previous_monitor_for_named_loop(
    otel_mock: otel.OTelMocker,
) -> None:
    previous = _asyncio.AsyncioMonitor("main")
    current = _asyncio.AsyncioMonitor("main")
    current._loop = object()  # type: ignore[assignment] # Controlled loop internals.

    previous_observations = tuple(previous._observe_ready_callbacks(None))  # pyright: ignore[reportPrivateUsage] # Duplicate-instrument regression.
    current_observations = tuple(current._observe_ready_callbacks(None))  # pyright: ignore[reportPrivateUsage] # Replacement lifecycle test.
    metrics = otel_mock.get_metrics_data()
    ready_callbacks = next(
        metric
        for metric in metrics
        if metric.name == "asyncio.event_loop.ready_callbacks"
    )

    assert previous_observations == ()
    assert current_observations[0].value == 0
    assert len(ready_callbacks.data.data_points) == 1
