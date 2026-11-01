"""Traced boundaries between an AnyIO event loop and its worker threads.

Use ``run_sync()`` from asynchronous code for blocking work that must run in a
worker thread. Use ``run_from_thread()`` only from work already running in an
AnyIO worker thread when it must call asynchronous code on the originating
event loop. Ordinary ``await`` calls and task spawning stay in the event loop
and do not use this module.
"""

from collections.abc import Awaitable, Callable
from threading import current_thread, get_ident
from typing import ParamSpec, TypeVar

from anyio import from_thread, to_thread
from opentelemetry import trace

P = ParamSpec("P")
T = TypeVar("T")

tracer = trace.get_tracer(__name__)


def _thread_attributes() -> dict[str, str | int]:
    thread = current_thread()
    return {
        "process.thread.id": get_ident(),
        "process.thread.name": thread.name,
    }


async def run_sync(
    name: str, func: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run blocking synchronous work from async code in a traced worker thread.

    Call from an AnyIO event-loop task for work such as synchronous database or
    HTTP clients. Do not use it for asynchronous callables or inexpensive,
    event-loop-safe work.
    """

    def traced() -> T:
        with tracer.start_as_current_span(
            f"THREAD {name}", attributes=_thread_attributes()
        ):
            return func(*args, **kwargs)

    return await to_thread.run_sync(traced)


def run_from_thread(
    name: str, func: Callable[P, Awaitable[T]], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Call async code from an AnyIO worker thread with a trace boundary.

    Call only inside a function invoked by ``run_sync()`` (or another AnyIO
    worker-thread API). Do not call it from an event-loop task; await the
    coroutine directly there instead.
    """

    async def traced() -> T:
        with tracer.start_as_current_span(
            f"THREAD {name}", attributes=_thread_attributes()
        ):
            return await func(*args, **kwargs)

    return from_thread.run(traced)
