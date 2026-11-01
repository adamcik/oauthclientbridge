from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol

from oauthclientbridge import types
from oauthclientbridge.errors import OAuthError


class OAuthOutcomeObserver(Protocol):
    def observe(
        self,
        endpoint: types.Endpoint,
        status: HTTPStatus,
        error: OAuthError | None,
    ) -> None: ...


class NullOAuthOutcomeObserver:
    def observe(
        self,
        endpoint: types.Endpoint,
        status: HTTPStatus,
        error: OAuthError | None,
    ) -> None:
        pass


class FallbackObserver(Protocol):
    def observe(self, endpoint: types.Endpoint, exception: BaseException) -> None: ...


class NullFallbackObserver:
    def observe(self, endpoint: types.Endpoint, exception: BaseException) -> None:
        pass


@dataclass(frozen=True)
class RequestLifecycleRequest:
    """Bounded request values supplied by a framework adapter."""

    attributes: dict[str, str | int | float | None]


@dataclass(frozen=True)
class RequestLifecycleResponseHeaders:
    """Bounded response headers supplied by a framework adapter."""

    content_type: str | None
    content_length: int | None
    cache_control: str | None


@dataclass(frozen=True)
class RequestLifecycleResponse:
    """Bounded final response values supplied by a framework adapter."""

    status_code: int
    body_size: int | None
    headers: RequestLifecycleResponseHeaders


class RequestLifecycleObserver(Protocol):
    def start(self, request: RequestLifecycleRequest) -> None: ...

    def complete(
        self, endpoint: types.Endpoint, response: RequestLifecycleResponse
    ) -> None: ...
