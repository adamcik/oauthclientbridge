import importlib.metadata
import ssl
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from http import HTTPStatus

import anyio
import httpx
from opentelemetry import trace

from oauthclientbridge import telemetry, types
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import ClientResetError, FetchSettings
from oauthclientbridge.utils import generations

from ._core import jitter_delay, parse_retry
from ._outcome import OAuthResponse, token_endpoint_outcome
from ._retry import (
    RetryCondition,
    RetryDecisionAction,
    get_retry_limiter,
)


def create_upstream_client(settings: FetchSettings) -> "HttpxUpstreamClient":
    return HttpxUpstreamClient(settings)


class HttpxUpstreamClient:
    def __init__(self, settings: FetchSettings) -> None:
        self._settings = settings
        self._fetches = _FetchTracker()
        self._clients = generations.Generations(
            lambda: _create_client(settings),
            _close_client,
            on_retired_drain=telemetry.observe_client_generation_drain_metric,
        )

    async def fetch(
        self,
        uri: str,
        upstream_grant_type: types.UpstreamGrantType,
        auth: str | None = None,
        **data: str | None,
    ) -> OAuthResponse:
        async with self._fetches.track():
            async with self._clients.acquire() as lease:
                return await _fetch(
                    lease,
                    self._clients,
                    self._settings,
                    uri,
                    upstream_grant_type,
                    data,
                    auth,
                )

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            await self._fetches.aclose(self._settings.total_timeout)
            await self._clients.aclose()


class _FetchTracker:
    """Track fetch cancellation scopes through bounded shutdown."""

    def __init__(self) -> None:
        self._lock = anyio.Lock()
        self._scopes: set[anyio.CancelScope] = set()
        self._drained = anyio.Event()
        self._drained.set()
        self._close_complete = anyio.Event()
        self._closing_started = anyio.Event()
        self._closing = False

    @asynccontextmanager
    async def track(self) -> AsyncGenerator[None]:
        scope = anyio.CancelScope()
        async with self._lock:
            if self._closing:
                raise RuntimeError("Upstream HTTP client is shutting down")
            if not self._scopes:
                self._drained = anyio.Event()
            self._scopes.add(scope)
        try:
            with scope:
                yield
            if scope.cancelled_caught:
                raise anyio.get_cancelled_exc_class()
        finally:
            with anyio.CancelScope(shield=True):
                async with self._lock:
                    self._scopes.remove(scope)
                    if not self._scopes:
                        self._drained.set()

    async def aclose(self, timeout: float) -> None:
        with anyio.CancelScope(shield=True):
            async with self._lock:
                if self._closing:
                    close_complete = self._close_complete
                    drained = None
                else:
                    self._closing = True
                    self._closing_started.set()
                    close_complete = None
                    drained = self._drained

            if close_complete is not None:
                await close_complete.wait()
                return

            assert drained is not None
            with anyio.move_on_after(timeout) as shutdown_timeout:
                await drained.wait()
            if shutdown_timeout.cancel_called:
                async with self._lock:
                    for scope in self._scopes:
                        scope.cancel()
            self._close_complete.set()


def _create_client(settings: FetchSettings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=False,
        headers={
            "User-Agent": "oauthclientbridge %s"
            % importlib.metadata.version("oauthclientbridge")
        },
        limits=httpx.Limits(
            max_connections=settings.pool_max_connections,
            max_keepalive_connections=settings.pool_max_keepalive_connections,
            keepalive_expiry=settings.pool_keepalive_expiry,
        ),
        timeout=httpx.Timeout(
            connect=settings.timeout,
            read=settings.timeout,
            write=settings.timeout,
            pool=settings.pool_timeout,
        ),
    )


async def _close_client(client: httpx.AsyncClient) -> None:
    await client.aclose()


async def _fetch(
    lease: generations.Lease[httpx.AsyncClient],
    clients: generations.Generations[httpx.AsyncClient],
    settings: FetchSettings,
    uri: str,
    upstream_grant_type: types.UpstreamGrantType,
    data: dict[str, str | None],
    auth: str | None = None,
) -> OAuthResponse:
    retry_limiter = get_retry_limiter(
        settings.retry_budget_capacity, settings.retry_budget_refill_per_initial
    )
    timed_out_result: OAuthResponse | None = None
    try:
        with anyio.fail_after(settings.total_timeout):
            deadline = anyio.current_time() + settings.total_timeout
            for attempt in range(settings.total_attempts):
                if attempt == 0:
                    retry_limiter.add(settings.retry_budget_refill_per_initial)
                retry_after_seconds = 0
                status: HTTPStatus | None = None
                reset_error: ClientResetError | None = None
                try:
                    if auth is None:
                        response = await lease.value.post(uri, data=data)
                    else:
                        response = await lease.value.post(
                            uri,
                            data=data,
                            auth=httpx.BasicAuth(auth, ""),
                        )
                except httpx.PoolTimeout:
                    telemetry.record_client_error_metric(
                        upstream_grant_type, None, "pool_saturation"
                    )
                    return OAuthError.TEMPORARILY_UNAVAILABLE.json(
                        description="Provider connection pool is unavailable."
                    )
                except httpx.ReadTimeout:
                    reset_error = ClientResetError.READ_TIMEOUT
                    result = OAuthError.TEMPORARILY_UNAVAILABLE.json(
                        description="Request timed out while reading from provider."
                    )
                    if (
                        upstream_grant_type
                        not in settings.read_timeout_retry_grant_types
                    ):
                        return result
                except httpx.ConnectTimeout:
                    reset_error = ClientResetError.CONNECTION_TIMEOUT
                    result = OAuthError.SERVER_ERROR.json(
                        description="Request timed out while connecting to provider."
                    )
                except httpx.TimeoutException:
                    result = OAuthError.SERVER_ERROR.json(
                        description="Request timed out while connecting to provider."
                    )
                except httpx.HTTPError as error:
                    reset_error = _reset_error_for_exception(error)
                    result = OAuthError.SERVER_ERROR.json(
                        description="An error occurred while connecting to the provider."
                    )
                else:
                    status = HTTPStatus(response.status_code)
                    reset_error = _reset_error_for_status(status)
                    retry_after_seconds = parse_retry(
                        response.headers.get("retry-after")
                    )
                    if response.is_redirect:
                        result = OAuthError.SERVER_ERROR.json(
                            description="Unhandled provider error (HTTP %s)."
                            % response.status_code
                        )
                    else:
                        try:
                            result = response.json()
                        except ValueError:
                            if status in settings.unavailable_status_codes:
                                result = OAuthError.TEMPORARILY_UNAVAILABLE.json(
                                    description="Provider is unavailable."
                                )
                            else:
                                result = OAuthError.SERVER_ERROR.json(
                                    description="Unhandled provider error (HTTP %s)."
                                    % response.status_code
                                )

                outcome = token_endpoint_outcome(
                    status,
                    result,
                    retry_status_codes=settings.retry_status_codes,
                    error_types=settings.error_types,
                )
                if not outcome.retryable:
                    if (
                        status is not None
                        and status.is_success
                        and outcome.normalized_error is None
                    ):
                        return result

                    description = result.get("error_description")
                    return (outcome.normalized_error or OAuthError.SERVER_ERROR).json(
                        description=description
                        if isinstance(description, str)
                        else None
                    )

                description = result.get("error_description")
                result = (outcome.normalized_error or OAuthError.SERVER_ERROR).json(
                    description=description if isinstance(description, str) else None
                )
                if retry_after_seconds:
                    result["retry_after"] = retry_after_seconds
                if attempt == settings.total_attempts - 1:
                    return result

                timed_out_result = result
                delay = (
                    retry_after_seconds
                    or (2 ** (attempt + 1) - 1) * settings.backoff_factor
                )
                sleep_for = 0.0
                if delay:
                    sleep_for = jitter_delay(
                        delay, settings, preserve_floor=retry_after_seconds > 0
                    )
                    if retry_after_seconds:
                        sleep_for = max(retry_after_seconds, sleep_for)
                if sleep_for > deadline - anyio.current_time():
                    _record_suppressed_retry(
                        upstream_grant_type,
                        RetryCondition.DEADLINE_EXCEEDED,
                    )
                    return result
                if not retry_limiter.consume():
                    _record_suppressed_retry(
                        upstream_grant_type,
                        RetryCondition.BUDGET_EXHAUSTED,
                    )
                    return result
                if (
                    reset_error is not None
                    and reset_error in settings.client_reset_errors
                ):
                    rotated = await clients.rotate(lease)
                    if rotated:
                        telemetry.record_client_generation_reset(
                            upstream_grant_type, reset_error
                        )
                if delay:
                    await _sleep(sleep_for)
    except TimeoutError:
        if timed_out_result is not None:
            return timed_out_result
        return OAuthError.SERVER_ERROR.json(
            description="Request timed out while connecting to provider."
        )

    return OAuthError.SERVER_ERROR.json(
        description="An unknown error occurred while talking to provider."
    )


def _record_suppressed_retry(
    upstream_grant_type: types.UpstreamGrantType, reason: RetryCondition
) -> None:
    telemetry.record_retry_decision_metric(
        upstream_grant_type, RetryDecisionAction.SKIP, reason
    )
    trace.get_current_span().add_event(
        "Retry suppressed",
        {
            "oauth.upstream_grant_type": str(upstream_grant_type),
            "retry.reason": str(reason),
        },
    )


async def _sleep(seconds: float) -> None:
    await anyio.sleep(seconds)


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
