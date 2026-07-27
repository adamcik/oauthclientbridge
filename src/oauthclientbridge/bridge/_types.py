from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TypeAlias

from oauthclientbridge.errors import OAuthError
from oauthclientbridge.types import JsonDict

Session: TypeAlias = dict[str, str]
# OAuth responses can contain credentials or authorization state. Keep the cache
# policy with their value type so every Bridge response producer gets it.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


@dataclass(frozen=True)
class BridgeResponse:
    """Framework-neutral OAuth response with safe default cache headers.

    Explicit headers override these defaults for the rare response that needs a
    more specific cache policy.
    """

    status: HTTPStatus
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    body: bytes | JsonDict = b""
    session: Session | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _NO_CACHE_HEADERS | dict(self.headers))


@dataclass(frozen=True)
class BridgeResult:
    """A completed Bridge response and its bounded OAuth classification.

    The response remains the sole client-visible error representation. The
    optional OAuth error is only for the endpoint execution seam to observe.
    Internal exception details never travel in a completed result.
    """

    response: BridgeResponse
    oauth_error: OAuthError | None
