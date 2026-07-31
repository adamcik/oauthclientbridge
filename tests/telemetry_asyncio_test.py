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
