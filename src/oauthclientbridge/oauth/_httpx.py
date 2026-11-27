import importlib.metadata
import ssl
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus

import anyio
import httpx
from opentelemetry import trace

from oauthclientbridge import telemetry, transport, types
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import (
    FetchSettings,
    FetchTransport,
)

from . import _httpx_attempts
from ._core import jitter_delay, parse_retry, record_fetch_metrics
from ._outcome import OAuthResponse, token_endpoint_outcome
from ._retry import (
    RetryCondition,
    RetryDecisionAction,
    get_retry_limiter,
)


def create_upstream_client(
    settings: FetchSettings,
    *,
    origin: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> "HttpxUpstreamClient":
    return HttpxUpstreamClient(settings, origin=origin, ssl_context=ssl_context)


class HttpxUpstreamClient:
    """Own outbound fetches; prepared mode must be started before serving traffic."""

    def __init__(
        self,
        settings: FetchSettings,
        *,
        origin: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._settings = settings
        self._fetches = _FetchTracker()
        self._http: _httpx_attempts.Backend
        if settings.transport == FetchTransport.PREPARED:
            if origin is None:
                raise ValueError("prepared transport requires a configured origin")
            prepared = transport.PreparedHTTPTransport(
                origin,
                versions=settings.prepared_versions,
                max_streams=settings.prepared_max_streams,
                connect_timeout=settings.timeout,
                ssl_context=ssl_context,
            )
            self._http = _httpx_attempts.PreparedHTTPX(
                prepared, _create_client(settings, prepared, ssl_context)
            )
        else:
            self._http = _httpx_attempts.StandardHTTPX(
                settings, lambda: _create_client(settings, None, ssl_context)
            )

    async def start(self) -> None:
        """Prepare verified TLS before traffic; standard HTTPX remains lazy."""
        await self._http.start()

    async def fetch(
        self,
        uri: str,
        upstream_grant_type: types.UpstreamGrantType,
        auth: str | None = None,
        **data: str | None,
    ) -> OAuthResponse:
        async with self._fetches.track(), self._http.acquire() as attempts:
            return await _fetch(
                attempts,
                self._settings,
                uri,
                upstream_grant_type,
                data,
                auth,
            )

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            await self._fetches.aclose(self._settings.total_timeout)
            await self._http.aclose()


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
                await drained.wait()
            self._close_complete.set()


def _create_client(
    settings: FetchSettings,
    prepared: transport.PreparedHTTPTransport | None,
    ssl_context: ssl.SSLContext | None,
) -> httpx.AsyncClient:
    instrumented_transport = None
    if prepared is not None:
        instrumented_transport = telemetry.instrument_httpx_transport(prepared)

    return httpx.AsyncClient(
        transport=instrumented_transport,
        verify=ssl_context if ssl_context is not None else True,
        trust_env=prepared is None,
        follow_redirects=False,
        headers={
            "User-Agent": "oauthclientbridge {}".format(
                importlib.metadata.version("oauthclientbridge")
            )
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


async def _fetch(
    attempts: _httpx_attempts.Attempts,
    settings: FetchSettings,
    uri: str,
    upstream_grant_type: types.UpstreamGrantType,
    data: dict[str, str | None],
    auth: str | None = None,
) -> OAuthResponse:
    start_time = time.monotonic()
    tracer = trace.get_tracer(__package__ or __name__)
    span = tracer.start_span(f"OAUTH {upstream_grant_type}")
    try:
        outcome = await _fetch_outcome(
            attempts,
            settings,
            uri,
            upstream_grant_type,
            data,
            auth,
        )
        record_fetch_metrics(
            span,
            upstream_grant_type,
            outcome.status,
            outcome.response,
            outcome.completed_retries,
            time.monotonic() - start_time,
        )
        return outcome.response
    finally:
        span.end()


@dataclass(frozen=True)
class _FetchOutcome:
    response: OAuthResponse
    status: HTTPStatus | None
    completed_retries: int


async def _fetch_outcome(
    attempts: _httpx_attempts.Attempts,
    settings: FetchSettings,
    uri: str,
    upstream_grant_type: types.UpstreamGrantType,
    data: dict[str, str | None],
    auth: str | None = None,
) -> _FetchOutcome:
    retry_limiter = get_retry_limiter(
        settings.retry_budget_capacity, settings.retry_budget_refill_per_initial
    )
    timed_out_result: OAuthResponse | None = None
    status: HTTPStatus | None = None
    completed_retries = 0
    try:
        with anyio.fail_after(settings.total_timeout):
            deadline = anyio.current_time() + settings.total_timeout
            for attempt in range(settings.total_attempts):
                completed_retries = attempt
                telemetry.record_client_attempt_metric(
                    upstream_grant_type,
                    types.RetryAttemptKind.INITIAL
                    if attempt == 0
                    else types.RetryAttemptKind.RETRY,
                )
                if attempt == 0:
                    retry_limiter.add(settings.retry_budget_refill_per_initial)
                retry_after_seconds = 0
                status = None
                attempt_result = await attempts.send(
                    uri, upstream_grant_type, data, auth
                )
                if isinstance(attempt_result, _httpx_attempts.Failure):
                    result = attempt_result.response
                    if not attempt_result.retryable:
                        return _FetchOutcome(result, status, completed_retries)
                else:
                    response = attempt_result
                    status = HTTPStatus(response.status_code)
                    retry_after_seconds = parse_retry(
                        response.headers.get("retry-after")
                    )
                    if response.is_redirect:
                        result = OAuthError.SERVER_ERROR.json(
                            description=f"Unhandled provider error (HTTP {response.status_code})."
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
                                    description=f"Unhandled provider error (HTTP {response.status_code})."
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
                        return _FetchOutcome(result, status, completed_retries)

                    description = result.get("error_description")
                    return _FetchOutcome(
                        (outcome.normalized_error or OAuthError.SERVER_ERROR).json(
                            description=description
                            if isinstance(description, str)
                            else None
                        ),
                        status,
                        completed_retries,
                    )

                description = result.get("error_description")
                result = (outcome.normalized_error or OAuthError.SERVER_ERROR).json(
                    description=description if isinstance(description, str) else None
                )
                if retry_after_seconds:
                    result["retry_after"] = retry_after_seconds
                if status is not None:
                    telemetry.record_client_error_metric(
                        upstream_grant_type,
                        status,
                        outcome.normalized_error or OAuthError.SERVER_ERROR,
                    )
                if attempt == settings.total_attempts - 1:
                    _record_suppressed_retry(
                        upstream_grant_type,
                        outcome.retry_condition or RetryCondition.UNKNOWN,
                    )
                    return _FetchOutcome(result, status, completed_retries)

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
                    return _FetchOutcome(result, status, completed_retries)
                if not retry_limiter.consume():
                    _record_suppressed_retry(
                        upstream_grant_type,
                        RetryCondition.BUDGET_EXHAUSTED,
                    )
                    return _FetchOutcome(result, status, completed_retries)
                telemetry.record_retry_decision_metric(
                    upstream_grant_type,
                    RetryDecisionAction.RETRY,
                    outcome.retry_condition or RetryCondition.UNKNOWN,
                )
                await attempts.prepare_retry(attempt_result, upstream_grant_type)
                if delay:
                    await _sleep(sleep_for)
    except TimeoutError:
        if timed_out_result is not None:
            return _FetchOutcome(timed_out_result, status, completed_retries)
        return _FetchOutcome(
            OAuthError.SERVER_ERROR.json(
                description="Request timed out while connecting to provider."
            ),
            status,
            completed_retries,
        )

    return _FetchOutcome(
        OAuthError.SERVER_ERROR.json(
            description="An unknown error occurred while talking to provider."
        ),
        status,
        completed_retries,
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
