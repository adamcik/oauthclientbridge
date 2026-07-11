"""Concurrency helpers for coalescing repeated background work requests."""

import threading
import time
from collections.abc import Callable

from opentelemetry import trace

tracer = trace.get_tracer(__name__)


class CoalescingWorker:
    """Run background work once for each burst of incoming refresh requests."""

    def __init__(
        self,
        work: Callable[[], None],
        *,
        debounce_seconds: float = 0.0,
        startup_delay: Callable[[], float] | None = None,
        name: str = "coalescing-worker",
    ) -> None:
        self._work = work
        self._debounce_seconds = debounce_seconds
        self._startup_delay = startup_delay
        self._name = name
        self._condition = threading.Condition()
        self._generation = 0
        self._handled_generation = 0
        self._started = False
        self._stopped = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._condition:
            if self._started:
                return

            self._started = True
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name=self._name,
            )
            self._thread.start()

    def request(self) -> None:
        with self._condition:
            self._generation += 1
            self._condition.notify()

    def stop(self, timeout: float | None = None) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        startup_delay = 0.0 if self._startup_delay is None else self._startup_delay()
        if not self._wait(startup_delay):
            return

        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: (
                        self._stopped or self._generation != self._handled_generation
                    )
                )
                if self._stopped:
                    return

                generation = self._generation

            if not self._wait(self._debounce_seconds):
                return

            with self._condition:
                if self._stopped:
                    return
                self._handled_generation = max(generation, self._generation)
                handled_generation = self._handled_generation

            with tracer.start_as_current_span(f"WORKER {self._name}") as span:
                span.set_attribute("worker.name", self._name)
                span.set_attribute("worker.debounce_seconds", self._debounce_seconds)
                span.set_attribute("worker.startup_delay_seconds", startup_delay)
                span.set_attribute("worker.request_generation", generation)
                span.set_attribute("worker.handled_generation", handled_generation)
                span.set_attribute(
                    "worker.coalesced_requests", handled_generation - generation + 1
                )
                self._work()

    def _wait(self, delay: float) -> bool:
        if delay <= 0:
            with self._condition:
                return not self._stopped

        deadline = time.monotonic() + delay
        with self._condition:
            while not self._stopped:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True
                self._condition.wait(timeout=remaining)

        return False
