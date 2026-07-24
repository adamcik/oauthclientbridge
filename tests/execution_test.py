import threading

import pytest
from opentelemetry import trace

from oauthclientbridge import execution

from .plugins import otel


@pytest.mark.anyio
async def test_run_sync_creates_worker_thread_span(otel_mock: otel.OTelMocker) -> None:
    _ = otel_mock
    caller_thread = threading.get_ident()

    def worker_details() -> tuple[int, str]:
        return threading.get_ident(), trace.get_current_span().name

    worker_thread, span_name = await execution.run_sync("sync work", worker_details)
    assert worker_thread != caller_thread
    assert span_name == "THREAD sync work"


@pytest.mark.anyio
async def test_run_from_thread_creates_event_loop_span(
    otel_mock: otel.OTelMocker,
) -> None:
    _ = otel_mock

    async def current_span_name() -> str:
        return trace.get_current_span().name

    span_name = await execution.run_sync(
        "from thread", execution.run_from_thread, "async work", current_span_name
    )

    assert span_name == "THREAD async work"
