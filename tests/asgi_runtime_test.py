import threading

import anyio
import pytest

from oauthclientbridge.asgi_context import TokenStateRefresher
from oauthclientbridge.settings import Settings


@pytest.mark.anyio
async def test_token_state_refresher_coalesces_requests_during_a_refresh(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    calls = 0
    max_active = 0
    first_started = anyio.Event()
    second_finished = anyio.Event()
    release_first = threading.Event()

    def token_state_counts(_: object) -> dict[str, int]:
        nonlocal active, calls, max_active
        active += 1
        max_active = max(max_active, active)
        calls += 1
        if calls == 1:
            anyio.from_thread.run_sync(first_started.set)
            assert release_first.wait(timeout=1)
        if calls == 2:
            anyio.from_thread.run_sync(second_finished.set)
        active -= 1
        return {"present": calls, "revoked": 0}

    monkeypatch.setattr(
        "oauthclientbridge.asgi_context.db.token_state_counts",
        token_state_counts,
    )
    refresher = TokenStateRefresher(settings.database)

    async with anyio.create_task_group() as task_group:
        await refresher.start(task_group)
        await first_started.wait()
        await refresher.request()
        await refresher.request()
        release_first.set()
        await second_finished.wait()
        task_group.cancel_scope.cancel()

    assert calls == 2
    assert max_active == 1
