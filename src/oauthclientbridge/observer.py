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
