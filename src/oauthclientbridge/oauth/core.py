import email.utils
import functools
import importlib.metadata
import random
import re
import time
from http import HTTPStatus
from typing import Any, override

import flask
import requests
import structlog
from opentelemetry import metrics, trace

from oauthclientbridge import telemetry
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import current_settings
from oauthclientbridge.utils import APIResult, http_status_to_result, rewrite_uri

from .outcome import OAuthResponse, token_endpoint_outcome
from .retry import (
    RetryAttemptKind,
    RetryDecision,
    RetryDecisionAction,
    RetryReason,
    get_retry_limiter,
)

logger: structlog.BoundLogger = structlog.get_logger()
_otel_scope = __package__ or __name__
tracer = trace.get_tracer(_otel_scope)
meter = metrics.get_meter(_otel_scope)
REDACTED_LOG_VALUE = "<REDACTED>"
ALLOWED_LOG_FIELDS = frozenset(
    {"client_id", "error", "error_description", "error_uri", "retry_after"}
)

_oauth_client_total_counter = meter.create_counter(
    name="oauth.client.total",
    description="Measures the total number of OAuth client requests.",
)

_oauth_client_duration_histogram = meter.create_histogram(
    name="oauth.client.duration",
    description="Measures the duration of an OAuth client request, including retries.",
    unit="s",
)

_oauth_client_retries_histogram = meter.create_histogram(
    name="oauth.client.retries",
    description="Measures the number of retries for an OAuth client request.",
    unit="1",
)

URIParam = dict[str, str]


@functools.lru_cache()
def get_session():
    session = requests.Session()
    session.headers["User-Agent"] = "oauthclientbridge %s" % importlib.metadata.version(
        "oauthclientbridge"
    )
    return session


class Error(Exception):
    def __init__(
        self,
        error: OAuthError,
        description: str | None = None,
        uri: str | None = None,
        retry_after: int | None = None,
    ):
        super().__init__()
        self.error: OAuthError = error
        self.description: str = description or error.description
        self.uri: str | None = uri
        self.retry_after: int | None = retry_after

    @override
    def __str__(self) -> str:
        return f"{self.error}: {self.description}"


def error_handler(e: Error) -> flask.Response:
    """Create a well formed JSON response with status and auth headers."""
    result: dict[str, str] = {
        "error": e.error.value,
        "error_description": e.description,
    }

    if e.uri is not None:
        result["error_uri"] = e.uri

    response = flask.jsonify(result)
    if e.error == OAuthError.INVALID_CLIENT:
        response.status_code = HTTPStatus.UNAUTHORIZED
        response.headers["WWW-Authenticate"] = (
            f'Basic realm="{current_settings.auth_realm}"'
        )
    elif e.error == OAuthError.TEMPORARILY_UNAVAILABLE:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
        if e.retry_after is not None:
            response.headers["Retry-After"] = int(e.retry_after)
    else:
        response.status_code = HTTPStatus.BAD_REQUEST

    current_span = trace.get_current_span()
    current_span.set_attribute("error.unhandled", False)
    current_span.set_attribute("oauth.error", e.error.value)
    structlog.contextvars.bind_contextvars(oauth_error=e.error.value)
    current_span.record_exception(e)
    current_span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))

    status = HTTPStatus(response.status_code)
    telemetry.record_server_error(status, e.error.value)
    return response


def fallback_error_handler(e: Exception) -> flask.Response:
    telemetry.record_server_error(
        HTTPStatus.INTERNAL_SERVER_ERROR, OAuthError.SERVER_ERROR.value
    )

    current_span = trace.get_current_span()
    current_span.set_attribute("error.unhandled", True)
    current_span.record_exception(e)
    current_span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))

    response = flask.jsonify(OAuthError.SERVER_ERROR.json())
    response.status_code = HTTPStatus.INTERNAL_SERVER_ERROR
    return response


def nocache(response: flask.Response) -> flask.Response:
    """Turns off caching in case there is sensitive content in responses."""
    if "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


def scrub_refresh_token(token: OAuthResponse) -> OAuthResponse:
    remove = ("access_token", "expires_in", "token_type")
    return {k: v for k, v in token.items() if k not in remove}


def sanitize_for_logging(payload: OAuthResponse) -> OAuthResponse:
    return {
        key: value if key in ALLOWED_LOG_FIELDS else REDACTED_LOG_VALUE
        for key, value in payload.items()
    }


def _record_attempt(endpoint: str, attempt_kind: RetryAttemptKind) -> None:
    telemetry.record_client_attempt(endpoint, attempt_kind)


def _record_retry_decision(endpoint: str, decision: RetryDecision) -> None:
    telemetry.record_retry_decision(endpoint, decision.action, decision.reason)


def fetch(uri: str, endpoint: str, auth: str | None = None, **data) -> OAuthResponse:
    """Perform post given URI with auth and provided data."""
    start_time = time.monotonic()
    with tracer.start_as_current_span(f"OAUTH {endpoint}") as span:
        req = requests.Request("POST", uri, data=data, auth=auth)
        prepared = req.prepare()
        retry_budget = _get_retry_limiter(
            current_settings.fetch.retry_budget_capacity,
            current_settings.fetch.retry_budget_refill_per_initial,
        )
        retry_budget.add(current_settings.fetch.retry_budget_refill_per_initial)

        deadline = time.monotonic() + current_settings.fetch.total_timeout
        retry = 0
        completed_retries = 0
        status: HTTPStatus | None = None
        pending_retry_decision: RetryDecision | None = None

        result: OAuthResponse = OAuthError.SERVER_ERROR.json(
            description="An unknown error occurred talking to provider."
        )

        for i in range(current_settings.fetch.total_retries + 1):
            prefix = "attempt #%d %s" % (i + 1, uri)
            backoff = (2**i - 1) * current_settings.fetch.backoff_factor
            remaining_timeout = deadline - time.monotonic()

            if pending_retry_decision is not None:
                if (retry or backoff) > remaining_timeout:
                    _record_retry_decision(
                        endpoint,
                        RetryDecision(
                            RetryDecisionAction.SKIP,
                            RetryReason.DEADLINE_EXCEEDED,
                        ),
                    )
                    span.add_event("No timeout remaining")
                    logger.debug("Abort %s no timeout remaining.", prefix)
                    break
                elif (retry or backoff) > 0:
                    if not retry_budget.consume():
                        _record_retry_decision(
                            endpoint,
                            RetryDecision(
                                RetryDecisionAction.SKIP,
                                RetryReason.RESOURCE_EXHAUSTED,
                            ),
                        )
                        span.add_event("Retry budget exhausted")
                        logger.debug("Abort %s retry budget exhausted.", prefix)
                        break

                    base_delay = retry if retry else backoff
                    sleep_for = jitter_delay(base_delay, preserve_floor=retry > 0)
                    if retry:
                        sleep_for = max(retry, sleep_for)
                    if sleep_for > remaining_timeout:
                        _record_retry_decision(
                            endpoint,
                            RetryDecision(
                                RetryDecisionAction.SKIP,
                                RetryReason.DEADLINE_EXCEEDED,
                            ),
                        )
                        span.add_event("No timeout remaining")
                        logger.debug("Abort %s no timeout remaining.", prefix)
                        break

                    _record_retry_decision(endpoint, pending_retry_decision)
                    span.add_event("Sleeping", {"duration": sleep_for})
                    logger.debug("Retry %s [sleep %.3f]", prefix, sleep_for)
                    time.sleep(sleep_for)
                    remaining_timeout = deadline - time.monotonic()
                    if remaining_timeout <= 0:
                        _record_retry_decision(
                            endpoint,
                            RetryDecision(
                                RetryDecisionAction.SKIP,
                                RetryReason.DEADLINE_EXCEEDED,
                            ),
                        )
                        span.add_event("No timeout remaining")
                        logger.debug("Abort %s no timeout remaining.", prefix)
                        break

                    completed_retries += 1

                pending_retry_decision = None

            _record_attempt(
                endpoint,
                RetryAttemptKind.RETRY
                if completed_retries > 0
                else RetryAttemptKind.INITIAL,
            )

            if remaining_timeout <= 0:
                _record_retry_decision(
                    endpoint,
                    RetryDecision(
                        RetryDecisionAction.SKIP,
                        RetryReason.DEADLINE_EXCEEDED,
                    ),
                )
                span.add_event("No timeout remaining")
                logger.debug("Abort %s no timeout remaining.", prefix)
                break

            result, status, retry = _fetch(
                span,
                prepared,
                remaining_timeout,
                endpoint,
            )

            outcome = token_endpoint_outcome(
                status,
                result,
                retry_status_codes=current_settings.fetch.retry_status_codes,
                error_types=current_settings.fetch.error_types,
                logger=logger,
                endpoint=endpoint,
            )

            if outcome.retryable:
                if status is None:
                    span.add_event("Missing response")
                else:
                    span.add_event("Retryable response", {"status": status})

                description = result.get("error_description")
                normalized_error = outcome.normalized_error or OAuthError.SERVER_ERROR
                result = normalized_error.json(description=description)
                pending_retry_decision = RetryDecision(
                    RetryDecisionAction.RETRY,
                    outcome.retry_reason or RetryReason.UNKNOWN,
                )
            elif status is not None and status.is_success:
                span.add_event("Success")
                pending_retry_decision = None
                break
            else:
                span.add_event(
                    "Aborted", {"status": int(status) if status is not None else -1}
                )
                pending_retry_decision = None
                break

            if status is not None and "error" in result:
                error_code = result["error"]
                if error_code in current_settings.fetch.error_types:
                    error_label = current_settings.fetch.error_types[error_code].value
                elif error_code in OAuthError:
                    error_label = OAuthError(error_code).value
                else:
                    error_label = "invalid_error"
                telemetry.record_client_error(endpoint, status, error_label)

            logger.debug(
                "Result %s [status %s] [retry after %s]", prefix, status, retry
            )
        else:
            if pending_retry_decision is not None:
                _record_retry_decision(
                    endpoint,
                    RetryDecision(
                        RetryDecisionAction.SKIP,
                        pending_retry_decision.reason,
                    ),
                )

        final_result = (
            APIResult.TIMEOUT if status is None else http_status_to_result(status)
        )

        attributes: dict[str, Any] = {
            "operation": endpoint,
            "final.result": final_result,
        }
        if status:
            attributes["http.response.status_code"] = int(status)

        telemetry.record_client_retries(endpoint, status, completed_retries)

        error_type = result.get("error")
        if error_type:
            attributes["error.type"] = error_type
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(result)))

        if "error" in result and retry:
            result["retry_after"] = retry

        span.set_attribute("total_retries", completed_retries)
        for key, value in attributes.items():
            span.set_attribute(key, value)

        duration = time.monotonic() - start_time
        _oauth_client_duration_histogram.record(duration, attributes)
        _oauth_client_retries_histogram.record(completed_retries, attributes)
        _oauth_client_total_counter.add(1, attributes)
        return result


def _fetch(
    span: trace.Span,
    prepared: requests.PreparedRequest,
    timeout: float,
    endpoint: str,
) -> tuple[OAuthResponse, HTTPStatus | None, int]:
    timeout = min(current_settings.fetch.timeout, timeout)
    start_time = time.time()

    session = get_session()
    status = None

    try:
        resp = session.send(prepared, timeout=timeout)
    except requests.exceptions.RequestException as e:
        request_latency = time.time() - start_time
        span.record_exception(e)
        span.add_event("Closing session to get new server")
        session.close()

        status_label = "unknown_exception"
        description = "An unknown error occurred while talking to provider."
        if isinstance(e, requests.exceptions.Timeout):
            description = "Request timed out while connecting to provider."
            if isinstance(e, requests.exceptions.ConnectTimeout):
                status_label = "connection_timeout"
            elif isinstance(e, requests.exceptions.ReadTimeout):
                status_label = "read_timeout"
        elif isinstance(e, requests.exceptions.ConnectionError):
            description = "An error occurred while connecting to the provider."
            if isinstance(e, requests.exceptions.SSLError):
                status_label = "ssl_error"
            elif isinstance(e, requests.exceptions.ProxyError):
                status_label = "proxy_error"
            else:
                status_label = "connection_error"

        if e.response:
            status = HTTPStatus(e.response.status_code)

        logger.warning("Fetching %r failed: %s", prepared.url, e)
        result = OAuthError.SERVER_ERROR.json(description=description)

        if isinstance(e, requests.exceptions.HTTPError):
            length = len(e.response.content)
            retry_after = parse_retry(e.response.headers.get("retry-after"))
        else:
            length = None
            retry_after = 0
    else:
        request_latency = time.time() - start_time

        result = _decode(span, resp)
        status = HTTPStatus(resp.status_code)
        status_label = status
        length = len(resp.content)
        retry_after = parse_retry(resp.headers.get("retry-after"))

        if status in current_settings.fetch.retry_status_codes:
            span.add_event("Closing session to get new server")
            session.close()

    telemetry.record_client_response(endpoint, status_label, request_latency, length)

    return result, status, retry_after


def _decode(span: trace.Span, resp: requests.Response) -> OAuthResponse:
    try:
        return resp.json()
    except ValueError as e:
        span.record_exception(e)

        logger.warning(
            "Fetching %r (HTTP %s, %s) failed: %s",
            resp.url,
            resp.status_code,
            resp.headers.get("Content-Type", "-"),
            e,
        )

    if resp.status_code in current_settings.fetch.unavailable_status_codes:
        error = OAuthError.TEMPORARILY_UNAVAILABLE
        description = "Provider is unavailable."
    else:
        error = OAuthError.SERVER_ERROR
        description = "Unhandled provider error (HTTP %s)." % resp.status_code

    return error.json(description=description)


def parse_retry(value: str | None) -> int:
    if not value:
        seconds = 0
    elif re.match(r"^\s*[0-9]+\s*$", value):
        seconds = int(value)
    else:
        parsed = email.utils.parsedate_tz(value)
        if parsed is None:
            seconds = 0
        else:
            seconds = int(email.utils.mktime_tz(parsed) - time.time())
    return max(0, seconds)


def jitter_delay(delay: float, preserve_floor: bool = False) -> float:
    """Apply jitter around a base delay.

    When preserving a provider-supplied floor like `Retry-After`, clamp the
    lower jitter bound to 1.0 so the full sampled range remains usable.
    """
    lower_bound = current_settings.fetch.backoff_jitter_min
    if preserve_floor:
        lower_bound = max(1.0, lower_bound)

    return delay * random.uniform(
        lower_bound,
        current_settings.fetch.backoff_jitter_max,
    )


def redirect(uri: str, **params: str) -> flask.Response:
    return flask.Response(
        status=HTTPStatus.FOUND,
        headers={"Location": rewrite_uri(uri, params)},
    )


# Preserve existing import/test seam while moving implementation to retry.py.
_get_retry_limiter = get_retry_limiter
