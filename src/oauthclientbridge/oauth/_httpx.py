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
from ._retry import RetryCondition, RetryDecisionAction, get_retry_limiter


def create_upstream_client(
    settings: FetchSettings,
    *,
    origin: str,
    ssl_context: ssl.SSLContext | None = None,
) -> "HttpxUpstreamClient":
    return HttpxUpstreamClient(settings, origin=origin, ssl_context=ssl_context)


class HttpxUpstreamClient:
    """Own prepared HTTPS, OAuth retries, and bounded fetch shutdown.

    Start before serving traffic. Each permitted HTTP-response retry requires a
    different connection; transport failures never authorize replay. TLS uses
    normal CA and hostname verification, not provider certificate pinning.
    A supplied SSL context belongs to this client.
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
            max_admission_age=settings.max_admission_age,
            prepare_ahead=settings.prepare_ahead,
            connect_timeout=settings.timeout,
            ssl_context=ssl_context,
        )
        instrumented_transport = telemetry.instrument_httpx_transport(self._transport)
        self._client = httpx.AsyncClient(
            transport=instrumented_transport,
            trust_env=False,
            follow_redirects=False,
            headers={
                "User-Agent": f"oauthclientbridge {importlib.metadata.version('oauthclientbridge')}"
            },
            timeout=httpx.Timeout(settings.timeout, pool=None),
        )
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
            started = time.monotonic()
            span = trace.get_tracer(__package__ or __name__).start_span(
                f"OAUTH {upstream_grant_type}"
            )
            try:
                outcome = await self._fetch_outcome(
                    uri, upstream_grant_type, data, auth
                )
                record_fetch_metrics(
                    span,
                    upstream_grant_type,
                    outcome.status,
                    outcome.response,
                    outcome.completed_retries,
                    time.monotonic() - started,
                )
                return outcome.response
            finally:
                span.end()

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            await self._fetches.aclose(self._settings.total_timeout)
            async with self._close_lock:
                await self._client.aclose()

    async def _fetch_outcome(
        self,
        uri: str,
        grant_type: types.UpstreamGrantType,
        data: dict[str, str | None],
        auth: str | None,
    ) -> "_FetchOutcome":
        settings = self._settings
        limiter = get_retry_limiter(
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
                        grant_type,
                        types.RetryAttemptKind.INITIAL
                        if attempt == 0
                        else types.RetryAttemptKind.RETRY,
                    )
                    if attempt == 0:
                        limiter.add(settings.retry_budget_refill_per_initial)
                    status = None
                    response = await self._send(uri, grant_type, data, auth)
                    if not isinstance(response, httpx.Response):
                        return _FetchOutcome(response, status, completed_retries)

                    status = HTTPStatus(response.status_code)
                    retry_after = parse_retry(response.headers.get("retry-after"))
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
                    if (
                        not outcome.retryable
                        and status.is_success
                        and outcome.normalized_error is None
                    ):
                        return _FetchOutcome(result, status, completed_retries)

                    description = result.get("error_description")
                    result = (outcome.normalized_error or OAuthError.SERVER_ERROR).json(
                        description=description
                        if isinstance(description, str)
                        else None
                    )
                    if not outcome.retryable:
                        return _FetchOutcome(result, status, completed_retries)

                    if retry_after:
                        result["retry_after"] = retry_after
                    telemetry.record_client_error_metric(
                        grant_type,
                        status,
                        outcome.normalized_error or OAuthError.SERVER_ERROR,
                    )
                    reason = outcome.retry_condition or RetryCondition.UNKNOWN
                    if attempt == settings.total_attempts - 1:
                        _record_suppressed_retry(grant_type, reason)
                        return _FetchOutcome(result, status, completed_retries)

                    timed_out_result = result
                    delay = (
                        retry_after
                        or (2 ** (attempt + 1) - 1) * settings.backoff_factor
                    )
                    sleep_for = 0.0
                    if delay:
                        sleep_for = jitter_delay(
                            delay, settings, preserve_floor=retry_after > 0
                        )
                        if retry_after:
                            sleep_for = max(retry_after, sleep_for)
                    if sleep_for > deadline - anyio.current_time():
                        _record_suppressed_retry(
                            grant_type, RetryCondition.DEADLINE_EXCEEDED
                        )
                        return _FetchOutcome(result, status, completed_retries)
                    if not limiter.consume():
                        _record_suppressed_retry(
                            grant_type, RetryCondition.BUDGET_EXHAUSTED
                        )
                        return _FetchOutcome(result, status, completed_retries)
                    telemetry.record_retry_decision_metric(
                        grant_type, RetryDecisionAction.RETRY, reason
                    )
                    if delay:
                        await _sleep(sleep_for)
                    try:
                        await self._transport.replace_for_retry(
                            response.extensions["prepared_connection_id"]
                        )
                    except httpx.HTTPError:
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

    async def _send(
        self,
        uri: str,
        grant_type: types.UpstreamGrantType,
        data: dict[str, str | None],
        auth: str | None,
    ) -> httpx.Response | OAuthResponse:
        """Send once; transport failures become terminal OAuth responses."""
        response_status: HTTPStatus | telemetry.ClientResponseStatus = (
            "unknown_exception"
        )
        response_size: int | None = None
        started = time.monotonic()
        try:
            authentication = httpx.USE_CLIENT_DEFAULT
            if auth is not None:
                authentication = httpx.BasicAuth(auth, "")
            response = await self._client.post(uri, data=data, auth=authentication)
            response_status = HTTPStatus(response.status_code)
            response_size = len(response.content)
            return response
        except transport.TransportNotReady:
            response_status = "transport_unavailable"
            telemetry.record_client_error_metric(
                grant_type, None, "transport_unavailable"
            )
            return OAuthError.TEMPORARILY_UNAVAILABLE.json(
                description="Provider prepared transport is unavailable."
            )
        except httpx.ReadTimeout:
            response_status = "read_timeout"
            return OAuthError.TEMPORARILY_UNAVAILABLE.json(
                description="Request timed out while reading from provider."
            )
        except httpx.TimeoutException:
            response_status = "connection_timeout"
            return OAuthError.SERVER_ERROR.json(
                description="Request timed out while connecting to provider."
            )
        except httpx.HTTPError as error:
            response_status = (
                "ssl_error" if _has_tls_cause(error) else "connection_error"
            )
            return OAuthError.SERVER_ERROR.json(
                description="An error occurred while connecting to the provider."
            )
        finally:
            telemetry.record_client_response_metric(
                grant_type, response_status, time.monotonic() - started, response_size
            )


@dataclass(frozen=True)
class _FetchOutcome:
    response: OAuthResponse
    status: HTTPStatus | None
    completed_retries: int


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


def _has_tls_cause(error: BaseException) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def _record_suppressed_retry(
    grant_type: types.UpstreamGrantType, reason: RetryCondition
) -> None:
    telemetry.record_retry_decision_metric(grant_type, RetryDecisionAction.SKIP, reason)
    trace.get_current_span().add_event(
        "Retry suppressed",
        {"oauth.upstream_grant_type": str(grant_type), "retry.reason": str(reason)},
    )


async def _sleep(seconds: float) -> None:
    await anyio.sleep(seconds)
