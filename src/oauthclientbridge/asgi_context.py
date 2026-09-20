import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from anyio import to_thread
from opentelemetry import trace

from oauthclientbridge import bridge, db, observer, telemetry
from oauthclientbridge.settings import DatabaseSettings, Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class TaskSpawner(Protocol):
    def start_soon(self, func: Callable[[], Awaitable[None]]) -> None: ...


class TokenStateRefresher:
    """Coalesce token-state metric updates in the ASGI application lifecycle."""

    def __init__(self, database: DatabaseSettings) -> None:
        self._database = database
        self._task_spawner: TaskSpawner | None = None
        self._requested = False
        self._running = False

    async def start(self, task_spawner: TaskSpawner) -> None:
        self._task_spawner = task_spawner
        await self.request()

    async def request(self) -> None:
        if self._task_spawner is None:
            return
        self._requested = True
        if self._running:
            return
        self._running = True
        self._task_spawner.start_soon(self._refresh)

    async def _refresh(self) -> None:
        try:
            while self._requested:
                self._requested = False
                with tracer.start_as_current_span("METRICS refresh"):
                    counts = await to_thread.run_sync(
                        db.token_state_counts, self._database
                    )
                    telemetry.set_token_state_counts_metric(counts)
        except Exception:
            logger.exception("Metrics refresh failed")
        finally:
            self._running = False


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    oauth_bridge: bridge.Bridge
    token_state_refresher: TokenStateRefresher
    fallback_observer: observer.FallbackObserver
    outcome_observer: observer.OAuthOutcomeObserver
