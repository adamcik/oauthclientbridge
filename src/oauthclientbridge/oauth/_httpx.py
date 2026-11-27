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
from oauthclientbridge.settings import FetchSettings

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
    origin: str,
    ssl_context: ssl.SSLContext | None = None,
) -> "HttpxUpstreamClient":
    return HttpxUpstreamClient(settings, origin=origin, ssl_context=ssl_context)


class HttpxUpstreamClient:
    """Own prepared upstream HTTPS; start before serving traffic.

    Only explicit HTTP responses can authorize retries, and each retry requires
    a different connection. A supplied SSL context belongs to this client.
    """

    def __init__(
        self,
        settings: FetchSettings,
        *,
        origin: str,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._settings = settings
        self._fetches = _FetchTracker()
        self._transport = transport.PreparedHTTPTransport(
            origin,
            versions=settings.versions,
            max_streams=settings.max_streams,
            connect_timeout=settings.timeout,
            ssl_context=ssl_context,
        )
        self._client = _create_client(settings, self._transport)
        self._close_lock = anyio.Lock()

    async def start(self) -> None:
        """Prepare verified TLS before traffic, without sending HTTP."""
        await self._transport.start()

    async def fetch(
        self,
        uri: str,
        upstream_grant_type: types.UpstreamGrantType,
        auth: str | None = None,
        **data: str | None,
    ) -> OAuthResponse:
        async with self._fetches.track():
            return await _fetch(
                self._client,
                self._transport,
                self._settings,
                uri,
                upstream_grant_type,
                data,
                auth,
            )

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            await self._fetches.aclose(self._settings.total_timeout)
            async with self._close_lock:
                await self._client.aclose()


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
    prepared: transport.PreparedHTTPTransport,
) -> httpx.AsyncClient:
    instrumented_transport = telemetry.instrument_httpx_transport(prepared)

    return httpx.AsyncClient(
        transport=instrumented_transport,
        trust_env=False,
        follow_redirects=False,
        headers={
            "User-Agent": "oauthclientbridge {}".format(
                importlib.metadata.version("oauthclientbridge")
            )
        },
        timeout=httpx.Timeout(
            connect=settings.timeout,
            read=settings.timeout,
            write=settings.timeout,
            pool=None,
        ),
    )


async def _fetch(
    client: httpx.AsyncClient,
    prepared: transport.PreparedHTTPTransport,
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
            client,
            prepared,
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


@dataclass(frozen=True)
class _Failure:
    response: OAuthResponse
    metric_status: telemetry.ClientResponseStatus


async def _send(
    client: httpx.AsyncClient,
    uri: str,
    grant_type: types.UpstreamGrantType,
    data: dict[str, str | None],
    auth: str | None,
) -> httpx.Response | _Failure:
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
        failure = _classify_failure(error)
        response_status = failure.metric_status
        if isinstance(error, transport.TransportNotReady):
            telemetry.record_client_error_metric(
                grant_type, None, "transport_unavailable"
            )
        return failure
    finally:
        telemetry.record_client_response_metric(
            grant_type, response_status, time.monotonic() - started, response_size
        )


def _classify_failure(error: httpx.HTTPError) -> _Failure:
    if isinstance(error, transport.TransportNotReady):
        return _Failure(
            response=OAuthError.TEMPORARILY_UNAVAILABLE.json(
                description="Provider prepared transport is unavailable."
            ),
            metric_status="transport_unavailable",
        )
    if isinstance(error, httpx.ReadTimeout):
        return _Failure(
            response=OAuthError.TEMPORARILY_UNAVAILABLE.json(
                description="Request timed out while reading from provider."
            ),
            metric_status="read_timeout",
        )
    if isinstance(error, httpx.TimeoutException):
        return _Failure(
            response=OAuthError.SERVER_ERROR.json(
                description="Request timed out while connecting to provider."
            ),
            metric_status="connection_timeout",
        )
    return _Failure(
        response=OAuthError.SERVER_ERROR.json(
            description="An error occurred while connecting to the provider."
        ),
        metric_status="ssl_error" if _has_tls_cause(error) else "connection_error",
    )


def _has_tls_cause(error: BaseException) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


async def _fetch_outcome(
    client: httpx.AsyncClient,
    prepared: transport.PreparedHTTPTransport,
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
                attempt_result = await _send(
                    client, uri, upstream_grant_type, data, auth
                )
                if isinstance(attempt_result, _Failure):
                    return _FetchOutcome(
                        attempt_result.response, status, completed_retries
                    )
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
                    if status.is_success and outcome.normalized_error is None:
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
                if delay:
                    await _sleep(sleep_for)
                try:
                    await prepared.replace_for_retry(
                        response.extensions["prepared_connection_id"]
                    )
                except httpx.HTTPError:
                    # Failed replacement must not replay on the preceding socket.
                    return _FetchOutcome(result, status, completed_retries)
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
