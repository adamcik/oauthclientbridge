"""Manage replaceable shared values while their active users drain safely.

Use ``Generations`` for long-lived resources, such as outbound clients, where a
new operation should use a replacement after a failure without interrupting
operations that still use the retired value.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Self

import anyio


class Generations[T]:
    """Own one current value while safely draining retired leased values.

    ``acquire()`` creates a lease for one operation. ``rotate()`` advances an
    active lease to the current replacement value. ``aclose()`` stops new
    leases, waits for active leases to release, then finalizes the current value.
    """

    def __init__(
        self,
        create: Callable[[], T],
        finalize: Callable[[T], Awaitable[None]],
    ) -> None:
        self._create = create
        self._finalize = finalize
        self._lock = anyio.Lock()
        self._current = _Generation(create())
        self._active_leases = 0
        self._leases_drained = anyio.Event()
        self._leases_drained.set()
        self._close_complete = anyio.Event()
        self._close_error: BaseException | None = None
        self._closing = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator["Lease[T]"]:
        """Yield a single-use asynchronous lease on the current value."""
        lease = Lease[T]()
        await self._acquire_lease(lease)
        try:
            yield lease
        finally:
            with anyio.CancelScope(shield=True):
                await self._release_lease(lease)

    async def _acquire_lease(self, lease: "Lease[T]") -> None:
        async with self._lock:
            if self._closing:
                raise RuntimeError("Generations is shutting down")
            if lease._used:  # pyright: ignore[reportPrivateUsage] # Lease coordination.
                raise RuntimeError("Generation lease has already been used")
            if self._active_leases == 0:
                self._leases_drained = anyio.Event()
            self._active_leases += 1
            self._current.leases += 1
            lease._generation = self._current  # pyright: ignore[reportPrivateUsage] # Lease coordination.
            lease.value = self._current.value
            lease._active = True  # pyright: ignore[reportPrivateUsage] # Lease coordination.
            lease._used = True  # pyright: ignore[reportPrivateUsage] # Lease coordination.

    async def rotate(self, lease: "Lease[T]") -> None:
        """Advance an active lease after its current value has failed."""
        finalize: _Generation[T] | None = None
        async with self._lock:
            if not lease._active:  # pyright: ignore[reportPrivateUsage] # Lease coordination.
                raise RuntimeError("Generation lease is not active")
            failed = lease._generation  # pyright: ignore[reportPrivateUsage] # Lease coordination.
            if failed is None:
                raise RuntimeError("Generation lease has no value")
            if failed is self._current:
                replacement = _Generation(self._create())
                self._current = replacement
            else:
                replacement = self._current
            failed.leases -= 1
            if failed.leases == 0 and failed is not self._current:
                finalize = failed
            replacement.leases += 1
            lease._generation = replacement  # pyright: ignore[reportPrivateUsage] # Lease coordination.
            lease.value = replacement.value
        if finalize is not None:
            await self._finalize(finalize.value)

    async def aclose(self) -> None:
        """Finalize values after all active leases have released."""
        leases_drained: anyio.Event | None = None
        async with self._lock:
            if self._closing:
                close_complete = self._close_complete
            else:
                self._closing = True
                close_complete = None
                leases_drained = self._leases_drained
        if close_complete is not None:
            await close_complete.wait()
            if self._close_error is not None:
                raise self._close_error
            return
        if leases_drained is None:
            raise RuntimeError("Generations did not start closing")
        try:
            await leases_drained.wait()
            await self._finalize(self._current.value)
        except BaseException as error:
            self._close_error = error
            raise
        finally:
            self._close_complete.set()

    async def _release_lease(self, lease: "Lease[T]") -> None:
        finalize: _Generation[T] | None = None
        async with self._lock:
            if not lease._active:  # pyright: ignore[reportPrivateUsage] # Lease coordination.
                raise RuntimeError("Generation lease is not active")
            lease._active = False  # pyright: ignore[reportPrivateUsage] # Lease coordination.
            generation = lease._generation  # pyright: ignore[reportPrivateUsage] # Lease coordination.
            if generation is None:
                raise RuntimeError("Generation lease has no value")
            generation.leases -= 1
            if generation.leases == 0 and generation is not self._current:
                finalize = generation
            lease._generation = None  # pyright: ignore[reportPrivateUsage] # Lease coordination.
            self._active_leases -= 1
            if self._active_leases == 0:
                self._leases_drained.set()
        if finalize is not None:
            await self._finalize(finalize.value)


class Lease[T]:
    """Opaque token representing one operation's currently leased value."""

    def __init__(self) -> None:
        self._generation: _Generation[T] | None = None
        self.value: T
        self._active = False
        self._used = False


@dataclass(eq=False)
class _Generation[T]:
    """Tracks a value's concurrent lease count until its final lease releases it."""

    value: T
    leases: int = 0
