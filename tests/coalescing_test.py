import threading
import time

from oauthclientbridge.coalescing import CoalescingWorker


def test_worker_respects_startup_delay_for_initial_request() -> None:
    ran = threading.Event()
    worker = CoalescingWorker(
        ran.set,
        startup_delay=lambda: 0.2,
    )

    worker.start()
    worker.request()

    assert not ran.wait(timeout=0.05)
    assert ran.wait(timeout=0.5)

    worker.stop(timeout=1.0)


def test_worker_coalesces_burst_requests() -> None:
    count = 0
    lock = threading.Lock()
    ran = threading.Event()

    def work() -> None:
        nonlocal count
        with lock:
            count += 1
        ran.set()

    worker = CoalescingWorker(work, debounce_seconds=0.05)
    worker.start()

    worker.request()
    worker.request()
    worker.request()

    assert ran.wait(timeout=0.5)
    time.sleep(0.1)

    with lock:
        assert count == 1

    worker.stop(timeout=1.0)


def test_worker_runs_again_for_request_during_work() -> None:
    count = 0
    lock = threading.Lock()
    first_run_started = threading.Event()
    release_first_run = threading.Event()
    second_run_finished = threading.Event()

    def work() -> None:
        nonlocal count
        with lock:
            count += 1
            current = count

        if current == 1:
            first_run_started.set()
            assert release_first_run.wait(timeout=0.5)
            return

        second_run_finished.set()

    worker = CoalescingWorker(work)
    worker.start()
    worker.request()

    assert first_run_started.wait(timeout=0.5)
    worker.request()
    release_first_run.set()

    assert second_run_finished.wait(timeout=0.5)

    with lock:
        assert count == 2

    worker.stop(timeout=1.0)
