import json
import logging
import threading

import pytest
from opentelemetry import trace

from oauthclientbridge import execution, logs
from oauthclientbridge.settings import LogSettings

from .plugins import otel


@pytest.mark.anyio
async def test_run_sync_creates_worker_thread_span(otel_mock: otel.OTelMocker) -> None:
    _ = otel_mock
    caller_thread = threading.get_ident()

    def worker_details() -> tuple[int, str, str, dict[str, object]]:
        thread = threading.current_thread()
        span = trace.get_current_span()
        return thread.ident or 0, thread.name, span.name, dict(span.attributes)

    worker_thread, worker_name, span_name, attributes = await execution.run_sync(
        "sync work", worker_details
    )
    assert worker_thread != caller_thread
    assert span_name == "THREAD sync work"
    assert attributes["process.thread.id"] == worker_thread
    assert attributes["process.thread.name"] == worker_name


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


@pytest.mark.anyio
async def test_run_sync_logs_worker_thread_attributes(
    instrumented: None, capsys: pytest.CaptureFixture[str]
) -> None:
    logs.init_logging(LogSettings(json_output=True))

    def log_from_worker() -> tuple[int, str]:
        thread = threading.current_thread()
        logging.getLogger(__name__).warning("worker log")
        return thread.ident or 0, thread.name

    worker_id, worker_name = await execution.run_sync("logging", log_from_worker)

    record = json.loads(capsys.readouterr().err)
    assert record["process.thread.id"] == worker_id
    assert record["process.thread.name"] == worker_name
