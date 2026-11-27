"""Own HTTP attempts and the connection policy behind them.

An Attempts session spans one logical fetch, including its admitted retries.
It returns responses or classified failures; retry permission is explicit rather
than inferred from an HTTPX exception by the OAuth retry loop. Only standard
HTTPX sessions rotate client generations. Prepared sessions retain one client
whose transport owns connection replacement and never authorize failure replay.
"""

import ssl
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from typing import Literal, Protocol

import anyio
import httpx

from oauthclientbridge import telemetry, transport, types
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import ClientResetError, FetchSettings
from oauthclientbridge.utils import generations

from ._outcome import OAuthResponse


@dataclass(frozen=True)
class Failure:
    response: OAuthResponse
    retryable: bool
    metric_status: telemetry.ClientResponseStatus
    reset_error: ClientResetError | None = None
    error_label: Literal["pool_saturation", "transport_unavailable"] | None = None


type Result = httpx.Response | Failure


class Attempts(Protocol):
    async def send(
        self,
        uri: str,
        grant_type: types.UpstreamGrantType,
        data: dict[str, str | None],
        auth: str | None,
    ) -> Result: ...

    async def prepare_retry(
        self, result: Result, grant_type: types.UpstreamGrantType
    ) -> None:
        """Apply connection policy only after the retry budget admits a retry."""
        ...


class Backend(Protocol):
    async def start(self) -> None: ...

    def acquire(self) -> AbstractAsyncContextManager[Attempts]: ...

    async def aclose(self) -> None: ...


class StandardHTTPX:
    def __init__(
        self, settings: FetchSettings, create_client: Callable[[], httpx.AsyncClient]
    ) -> None:
        self._settings = settings
        self._clients = generations.Generations(
            create_client,
            _close_client,
            on_retired_drain=telemetry.observe_client_generation_drain_metric,
        )

    async def start(self) -> None:
        pass

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Attempts]:
        async with self._clients.acquire() as lease:
            yield _StandardAttempts(self._settings, self._clients, lease)

    async def aclose(self) -> None:
        await self._clients.aclose()


class PreparedHTTPX:
    def __init__(
        self, prepared: transport.PreparedHTTPTransport, client: httpx.AsyncClient
    ) -> None:
        self._prepared = prepared
        self._client = client
        self._close_lock = anyio.Lock()

    async def start(self) -> None:
        await self._prepared.start()

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Attempts]:
        # The upstream client's fetch tracker drains callers before closing us;
        # the prepared transport owns physical connections and their leases.
        yield self

    async def aclose(self) -> None:
        async with self._close_lock:
            await self._client.aclose()

    async def send(
        self,
        uri: str,
        grant_type: types.UpstreamGrantType,
        data: dict[str, str | None],
        auth: str | None,
    ) -> Result:
        return await _send(self._client, uri, grant_type, data, auth, _terminal_failure)

    async def prepare_retry(
        self, result: Result, grant_type: types.UpstreamGrantType
    ) -> None:
        # Explicit HTTP retries do not retire healthy prepared connections.
        pass


class _StandardAttempts:
    def __init__(
        self,
        settings: FetchSettings,
        clients: generations.Generations[httpx.AsyncClient],
        lease: generations.Lease[httpx.AsyncClient],
    ) -> None:
        self._settings = settings
        self._clients = clients
        self._lease = lease

    async def send(
        self,
        uri: str,
        grant_type: types.UpstreamGrantType,
        data: dict[str, str | None],
        auth: str | None,
    ) -> Result:
        return await _send(
            self._lease.value, uri, grant_type, data, auth, self._failure
        )

    async def prepare_retry(
        self, result: Result, grant_type: types.UpstreamGrantType
    ) -> None:
        if isinstance(result, Failure):
            reset_error = result.reset_error
        else:
            reset_error = _reset_error_for_status(HTTPStatus(result.status_code))
        if (
            reset_error is not None
            and reset_error in self._settings.client_reset_errors
            and await self._clients.rotate(self._lease)
        ):
            telemetry.record_client_generation_reset(grant_type, reset_error)

    def _failure(
        self, error: httpx.HTTPError, grant_type: types.UpstreamGrantType
    ) -> Failure:
        retryable = not isinstance(error, httpx.ReadTimeout) or (
            grant_type in self._settings.read_timeout_retry_grant_types
        )
        return _classify_failure(error, retryable=retryable)


async def _close_client(client: httpx.AsyncClient) -> None:
    await client.aclose()


async def _send(
    client: httpx.AsyncClient,
    uri: str,
    grant_type: types.UpstreamGrantType,
    data: dict[str, str | None],
    auth: str | None,
    classify: Callable[[httpx.HTTPError, types.UpstreamGrantType], Failure],
) -> Result:
    response_status: HTTPStatus | telemetry.ClientResponseStatus = "unknown_exception"
    response_size: int | None = None
    started = time.monotonic()
    try:
        if auth is None:
            response = await client.post(uri, data=data)
        else:
            response = await client.post(uri, data=data, auth=httpx.BasicAuth(auth, ""))
        response_status = HTTPStatus(response.status_code)
        response_size = len(response.content)
        return response
    except httpx.HTTPError as error:
        failure = classify(error, grant_type)
        response_status = failure.metric_status
        if failure.error_label is not None:
            telemetry.record_client_error_metric(grant_type, None, failure.error_label)
        return failure
    finally:
        telemetry.record_client_response_metric(
            grant_type, response_status, time.monotonic() - started, response_size
        )


def _terminal_failure(
    error: httpx.HTTPError, grant_type: types.UpstreamGrantType
) -> Failure:
    return _classify_failure(error, retryable=False)


def _classify_failure(error: httpx.HTTPError, *, retryable: bool) -> Failure:
    if isinstance(error, transport.TransportNotReady):
        return Failure(
            response=OAuthError.TEMPORARILY_UNAVAILABLE.json(
                description="Provider prepared transport is unavailable."
            ),
            retryable=False,
            metric_status="transport_unavailable",
            error_label="transport_unavailable",
        )
    if isinstance(error, httpx.PoolTimeout):
        return Failure(
            response=OAuthError.TEMPORARILY_UNAVAILABLE.json(
                description="Provider connection pool is unavailable."
            ),
            retryable=False,
            metric_status="unknown_exception",
            error_label="pool_saturation",
        )
    if isinstance(error, httpx.ReadTimeout):
        return Failure(
            response=OAuthError.TEMPORARILY_UNAVAILABLE.json(
                description="Request timed out while reading from provider."
            ),
            retryable=retryable,
            metric_status="read_timeout",
            reset_error=ClientResetError.READ_TIMEOUT,
        )
    if isinstance(error, httpx.TimeoutException):
        reset_error = None
        if isinstance(error, httpx.ConnectTimeout):
            reset_error = ClientResetError.CONNECTION_TIMEOUT
        return Failure(
            response=OAuthError.SERVER_ERROR.json(
                description="Request timed out while connecting to provider."
            ),
            retryable=retryable,
            metric_status="connection_timeout",
            reset_error=reset_error,
        )
    return Failure(
        response=OAuthError.SERVER_ERROR.json(
            description="An error occurred while connecting to the provider."
        ),
        retryable=retryable,
        metric_status="ssl_error" if _has_tls_cause(error) else "connection_error",
        reset_error=_reset_error_for_exception(error),
    )


def _reset_error_for_status(status: HTTPStatus) -> ClientResetError | None:
    return {
        HTTPStatus.INTERNAL_SERVER_ERROR: ClientResetError.HTTP_500,
        HTTPStatus.BAD_GATEWAY: ClientResetError.HTTP_502,
        HTTPStatus.SERVICE_UNAVAILABLE: ClientResetError.HTTP_503,
        HTTPStatus.GATEWAY_TIMEOUT: ClientResetError.HTTP_504,
    }.get(status)


def _reset_error_for_exception(error: httpx.HTTPError) -> ClientResetError | None:
    if _has_tls_cause(error):
        return ClientResetError.TLS_ERROR
    if isinstance(error, httpx.ConnectError):
        return ClientResetError.CONNECTION_ERROR
    return None


def _has_tls_cause(error: BaseException) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return True
        cause = cause.__cause__ or cause.__context__
    return False
