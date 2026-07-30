import asyncio

import pytest
from opentelemetry.sdk.metrics.export import HistogramDataPoint, NumberDataPoint

from oauthclientbridge.telemetry import _asyncio

from .plugins import otel


@pytest.mark.anyio
async def test_asyncio_monitor_reports_named_loop_health(
    otel_mock: otel.OTelMocker,
) -> None:
    monitor = _asyncio.AsyncioMonitor("main")
    loop = asyncio.get_running_loop()
    monitor._loop = loop  # pyright: ignore[reportPrivateUsage] # Implementation test.
    loop.call_soon(lambda: None)
    loop.call_later(60, lambda: None)
    cancelled = loop.call_later(60, lambda: None)
    cancelled.cancel()

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
