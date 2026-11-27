"""Immutable snapshots of connection resources and caller-owned leases.

Socket cleanup removes a connection from inventory, not its outstanding response
leases. Busy connections may still admit streams: available_streams reports the
local admission allowance, not peer SETTINGS or a guarantee of future liveness.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

type HTTPVersion = Literal["HTTP/2", "HTTP/1.1"]


class Clock(Protocol):
    """Monotonic time with cancellable deadline waits."""

    def now(self) -> float: ...

    async def sleep_until(self, deadline: float) -> None: ...


@dataclass(frozen=True)
class ConnectionState:
    id: int
    leases: int
    retiring: bool
    admission_age: float
    version: HTTPVersion | None = None


@dataclass(frozen=True)
class TransportState:
    current_connection_id: int | None
    connections: tuple[ConnectionState, ...]
    preparing: bool
    closing: bool
    active_leases: int
    available_streams: int


type EventKind = Literal[
    "prepare_started",
    "published",
    "prepare_failed",
    "retired",
    "acquired",
    "released",
    "closed",
    "forced_closed",
    "close_failed",
    "maintenance_failure",
]
type Reason = Literal[
    "",
    "startup",
    "planned",
    "scheduled",
    "refill",
    "problem",
    "age_limit",
    "peer_closure",
    "operation_failure",
    "response_abandoned",
    "shutdown",
]
type Phase = Literal["tcp", "tls"]


@dataclass(frozen=True)
class TransportEvent:
    kind: EventKind
    connection_id: int | None
    reason: Reason
    state: TransportState
