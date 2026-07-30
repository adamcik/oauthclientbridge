"""Asyncio event-loop health metrics."""

import asyncio
import logging
from collections.abc import Iterable, Sized
from typing import Protocol, cast

import anyio
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

logger = logging.getLogger(__name__)

_WATCHDOG_INTERVAL_SECONDS = 1.0


class _ScheduledHandle(Protocol):
    def cancelled(self) -> bool: ...


class _LoopInternals(Protocol):
    _ready: Sized
    _scheduled: Iterable[_ScheduledHandle]


class AsyncioMonitor:
    """Collect health signals for one explicitly named asyncio event loop."""

    def __init__(self, name: str) -> None:
        self._attributes = {"asyncio.loop.name": name}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._introspection_enabled = True
        self._ready_callbacks = 0
        self._scheduled_callbacks = 0
        self._active_tasks = 0
        self._cancelling_tasks = 0

        meter = metrics.get_meter(__name__)
        self._schedule_delay = meter.create_histogram(
            "asyncio.event_loop.schedule_delay",
            description="Delay between a watchdog deadline and its execution.",
            unit="s",
        )
        self._heartbeat = meter.create_counter(
            "asyncio.event_loop.heartbeat",
            description="Completed event-loop watchdog ticks.",
            unit="{tick}",
        )
        meter.create_observable_gauge(
            "asyncio.event_loop.ready_callbacks",
            callbacks=[self._observe_ready_callbacks],
            description="Callbacks ready to run in the event loop.",
            unit="{callback}",
        )
        meter.create_observable_gauge(
            "asyncio.event_loop.scheduled_callbacks",
            callbacks=[self._observe_scheduled_callbacks],
            description="Non-cancelled delayed callbacks in the event loop.",
            unit="{callback}",
        )
        meter.create_observable_gauge(
            "asyncio.tasks.active",
            callbacks=[self._observe_active_tasks],
            description="Non-completed tasks attached to the event loop.",
            unit="{task}",
        )
        meter.create_observable_gauge(
            "asyncio.tasks.cancelling",
            callbacks=[self._observe_cancelling_tasks],
            description="Active tasks with cancellation requested.",
            unit="{task}",
        )

    async def run(self) -> None:
        """Run the watchdog until the enclosing task group cancels it."""
        self._loop = asyncio.get_running_loop()
        deadline = anyio.current_time() + _WATCHDOG_INTERVAL_SECONDS
        while True:
            await anyio.sleep_until(deadline)
            now = anyio.current_time()
            self._record_tick(max(now - deadline, 0))
            self._snapshot()

            # Preserve a fixed cadence without producing a burst after a stall.
            deadline += _WATCHDOG_INTERVAL_SECONDS
            while deadline <= now:
                deadline += _WATCHDOG_INTERVAL_SECONDS

    def _record_tick(self, delay: float) -> None:
        self._schedule_delay.record(delay, self._attributes)
        self._heartbeat.add(1, self._attributes)

    def _snapshot(self) -> None:
        if not self._introspection_enabled or self._loop is None:
            return

        try:
            loop = cast(_LoopInternals, self._loop)
            self._ready_callbacks = len(
                loop._ready  # pyright: ignore[reportPrivateUsage] # CPython compatibility boundary.
            )
            self._scheduled_callbacks = sum(
                not handle.cancelled()
                for handle in loop._scheduled  # pyright: ignore[reportPrivateUsage] # CPython compatibility boundary.
            )
            tasks = asyncio.all_tasks(self._loop)
            self._active_tasks = len(tasks)
            self._cancelling_tasks = sum(task.cancelling() > 0 for task in tasks)
        except (AttributeError, TypeError):
            self._introspection_enabled = False
            logger.warning(
                "Asyncio event-loop introspection disabled: unsupported loop internals"
            )

    def _observe_ready_callbacks(self, _: CallbackOptions) -> Iterable[Observation]:
        return self._observe(self._ready_callbacks)

    def _observe_scheduled_callbacks(self, _: CallbackOptions) -> Iterable[Observation]:
        return self._observe(self._scheduled_callbacks)

    def _observe_active_tasks(self, _: CallbackOptions) -> Iterable[Observation]:
        return self._observe(self._active_tasks)

    def _observe_cancelling_tasks(self, _: CallbackOptions) -> Iterable[Observation]:
        return self._observe(self._cancelling_tasks)

    def _observe(self, value: int) -> Iterable[Observation]:
        if self._loop is None or not self._introspection_enabled:
            return ()
        return (Observation(value, self._attributes),)
