import importlib.metadata
from http import HTTPStatus

import anyio
import httpx

from oauthclientbridge import telemetry, types
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import FetchSettings

from ._core import jitter_delay, parse_retry
from ._outcome import OAuthResponse, token_endpoint_outcome
from ._retry import (
    RetryCondition,
    RetryDecisionAction,
    get_retry_limiter,
)


class HttpxUpstreamClient:
    def __init__(self, settings: FetchSettings) -> None:
        self._settings = settings
        self._lock = anyio.Lock()
        self._fetch_scopes: set[anyio.CancelScope] = set()
        self._fetches_drained = anyio.Event()
        self._fetches_drained.set()
        self._close_complete = anyio.Event()
        self._closing = False
        self._client = httpx.AsyncClient(
            follow_redirects=False,
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

    async def fetch(
        self,
        uri: str,
        upstream_grant_type: types.UpstreamGrantType,
        auth: str | None = None,
        **data: str | None,
    ) -> OAuthResponse:
        fetch_scope = anyio.CancelScope()
        result: OAuthResponse | None = None
        async with self._lock:
            if self._closing:
                raise RuntimeError("Upstream HTTP client is shutting down")
            if not self._fetch_scopes:
                self._fetches_drained = anyio.Event()
            self._fetch_scopes.add(fetch_scope)

        try:
            with fetch_scope:
                result = await _fetch(
                    self._client, self._settings, uri, upstream_grant_type, data, auth
                )
            if fetch_scope.cancelled_caught:
                raise anyio.get_cancelled_exc_class()
            assert result is not None
            return result
        finally:
            with anyio.CancelScope(shield=True):
                async with self._lock:
                    self._fetch_scopes.remove(fetch_scope)
                    if not self._fetch_scopes:
                        self._fetches_drained.set()

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            fetches_drained: anyio.Event | None = None
            async with self._lock:
                if self._closing:
                    close_complete = self._close_complete
                else:
                    self._closing = True
                    close_complete = None
                    fetches_drained = self._fetches_drained

            if close_complete is not None:
                await close_complete.wait()
                return

            assert fetches_drained is not None
            with anyio.move_on_after(self._settings.total_timeout) as shutdown_timeout:
                await fetches_drained.wait()
            if shutdown_timeout.cancel_called:
                async with self._lock:
                    for fetch_scope in self._fetch_scopes:
                        fetch_scope.cancel()

            await self._client.aclose()
            self._close_complete.set()


def create_upstream_client(settings: FetchSettings) -> HttpxUpstreamClient:
    return HttpxUpstreamClient(settings)


async def _fetch(
    client: httpx.AsyncClient,
    settings: FetchSettings,
    uri: str,
    upstream_grant_type: types.UpstreamGrantType,
    data: dict[str, str | None],
    auth: str | None = None,
) -> OAuthResponse:
    _ = upstream_grant_type
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
                try:
                    headers = {
                        "User-Agent": "oauthclientbridge %s"
                        % importlib.metadata.version("oauthclientbridge")
                    }
                    if auth is None:
                        response = await client.post(uri, data=data, headers=headers)
                    else:
                        response = await client.post(
                            uri,
                            data=data,
                            auth=httpx.BasicAuth(auth, ""),
                            headers=headers,
                        )
                except httpx.PoolTimeout:
                    return OAuthError.TEMPORARILY_UNAVAILABLE.json(
                        description="Provider connection pool is unavailable."
                    )
                except httpx.ReadTimeout:
                    result = OAuthError.TEMPORARILY_UNAVAILABLE.json(
                        description="Request timed out while reading from provider."
                    )
                    if (
                        upstream_grant_type
                        not in settings.read_timeout_retry_grant_types
                    ):
                        return result
                except httpx.TimeoutException:
                    result = OAuthError.SERVER_ERROR.json(
                        description="Request timed out while connecting to provider."
                    )
                except httpx.HTTPError:
                    result = OAuthError.SERVER_ERROR.json(
                        description="An error occurred while connecting to the provider."
                    )
                else:
                    status = HTTPStatus(response.status_code)
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
                        telemetry.record_retry_decision_metric(
                            upstream_grant_type,
                            RetryDecisionAction.SKIP,
                            RetryCondition.DEADLINE_EXCEEDED,
                        )
                        return result
                if not retry_limiter.consume():
                    telemetry.record_retry_decision_metric(
                        upstream_grant_type,
                        RetryDecisionAction.SKIP,
                        RetryCondition.BUDGET_EXHAUSTED,
                    )
                    return result
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


async def _sleep(seconds: float) -> None:
    await anyio.sleep(seconds)
