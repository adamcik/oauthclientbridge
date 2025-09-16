import email.utils
import functools
import importlib.metadata
import re
import time
from http import HTTPStatus
from typing import Any, override

import flask
import requests
import structlog
from opentelemetry import metrics, trace

from oauthclientbridge import stats
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import current_settings
from oauthclientbridge.utils import APIResult, http_status_to_result, rewrite_uri

logger: structlog.BoundLogger = structlog.get_logger()
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

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

# TODO: This should be a stricter type or a pydantic model
OAuthResponse = dict[str, Any]
URIParam = dict[str, str]

# https://tools.ietf.org/html/rfc6749#section-4.1.2.1
AUTHORIZATION_ERRORS = {
    OAuthError.INVALID_REQUEST,
    OAuthError.UNAUTHORIZED_CLIENT,
    OAuthError.ACCESS_DENIED,
    OAuthError.UNSUPPORTED_RESPONSE_TYPE,
    OAuthError.INVALID_SCOPE,
    OAuthError.SERVER_ERROR,
    OAuthError.TEMPORARILY_UNAVAILABLE,
}

# https://tools.ietf.org/html/rfc6749#section-5.2
TOKEN_ERRORS = {
    OAuthError.INVALID_REQUEST,
    OAuthError.INVALID_CLIENT,
    OAuthError.INVALID_GRANT,
    OAuthError.UNAUTHORIZED_CLIENT,
    OAuthError.UNSUPPORTED_GRANT_TYPE,
    OAuthError.INVALID_SCOPE,
    # These are not really supported by RFC:
    OAuthError.SERVER_ERROR,
    OAuthError.TEMPORARILY_UNAVAILABLE,
}


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
        # TODO: This triggers a login prompt when testing, we probably don't want that.
        response.headers["WWW-Authenticate"] = (
            f'Basic realm="{current_settings.auth_realm}"'
        )
    elif e.retry_after:
        response.headers["Retry-After"] = int(e.retry_after + 1)
        response.status_code = HTTPStatus.TOO_MANY_REQUESTS
    else:
        response.status_code = HTTPStatus.BAD_REQUEST

    current_span = trace.get_current_span()
    current_span.record_exception(e)
    current_span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))

    status = HTTPStatus(response.status_code)
    stats.ServerErrorCounter.labels(
        endpoint=stats.endpoint(),
        status=stats.status(status),
        error=e.error,
    ).inc()
    return response


def fallback_error_handler(e: Exception) -> flask.Response:
    stats.ServerErrorCounter.labels(
        endpoint=stats.endpoint(),
        status=stats.status(HTTPStatus.INTERNAL_SERVER_ERROR),
        error=OAuthError.SERVER_ERROR.value,
    ).inc()

    current_span = trace.get_current_span()
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


def normalize_error(
    error_code: str, allowed_types: set[OAuthError], fallback_type: OAuthError
) -> OAuthError:
    """Translate any "bad" error types to something more usable."""
    if error_code in current_settings.fetch.error_types:
        error = current_settings.fetch.error_types[error_code]
    elif error_code in OAuthError:
        error = OAuthError(error_code)
    else:
        error = fallback_type

    if error not in allowed_types:
        return fallback_type
    return error


def validate_token(token: OAuthResponse) -> bool:
    return bool(token.get("access_token") and token.get("token_type"))


def scrub_refresh_token(token: OAuthResponse) -> OAuthResponse:
    remove = ("access_token", "expires_in", "token_type")
    return {k: v for k, v in token.items() if k not in remove}


# TODO: Turn endpoint into a StrEnum
def fetch(uri: str, endpoint: str, auth: str | None = None, **data) -> OAuthResponse:
    """Perform post given URI with auth and provided data."""
    start_time = time.monotonic()
    with tracer.start_as_current_span(f"OAUTH {endpoint}") as span:
        req = requests.Request("POST", uri, data=data, auth=auth)
        prepared = req.prepare()

        timeout = time.time() + current_settings.fetch.total_timeout
        retry = 0
        status: HTTPStatus | None = None  # Set a default value for status

        result = OAuthError.SERVER_ERROR.json(
            description="An unknown error occurred talking to provider."
        )

        i = 0
        for i in range(current_settings.fetch.total_retries):
            prefix = "attempt #%d %s" % (i + 1, uri)

            # TODO: Add jitter to backoff and/or retry after?
            backoff = (2**i - 1) * current_settings.fetch.backoff_factor
            remaining_timeout = timeout - time.time()

            if (retry or backoff) > remaining_timeout:
                span.add_event("No timeout remaining")
                logger.debug("Abort %s no timeout remaining.", prefix)
                # TODO: This should probably be a timeout outcome.
                break
            elif (retry or backoff) > 0:
                span.add_event("Sleeping", {"duration": retry or backoff})
                logger.debug("Retry %s [sleep %.3f]", prefix, retry or backoff)
                time.sleep(retry or backoff)

            result, status, retry = _fetch(
                span,
                prepared,
                remaining_timeout,
                endpoint,
            )

            labels = {
                "endpoint": endpoint,
                "status": stats.status(status) if status else "unknown",
            }
            stats.ClientRetryHistogram.labels(**labels).observe(i)

            if status is not None and "error" in result:
                error_code = result["error"]
                if error_code in current_settings.fetch.error_types:
                    error_label = current_settings.fetch.error_types[error_code].value
                elif error_code in OAuthError:
                    error_label = OAuthError(error_code).value
                else:
                    error_label = "invalid_error"
                stats.ClientErrorCounter.labels(error=error_label, **labels).inc()

            if status is None:
                span.add_event("Missing response")
                pass  # We didn't even get a response, so try again.
            elif status.is_success:
                span.add_event("Success")
                break
            elif status not in current_settings.fetch.retry_status_codes:
                span.add_event("Aborted", {"status": status})
                break
            elif "error" not in result:
                span.add_event("Non-OK without error!?")
                break  # No error reported so might as well return it.

            # TODO: Call out other inconsistent mixes/states?
            # TODO: Cleanup this logging to use structlog or tracing
            logger.debug(
                "Result %s [status %s] [retry after %s]", prefix, status, retry
            )

        if status is None:
            final_result = APIResult.TIMEOUT
        else:
            final_result = http_status_to_result(status)

        attributes: dict[str, Any] = {
            "operation": endpoint,
            "final.result": final_result,
        }
        if status:
            attributes["http.response.status_code"] = status

        error_type = result.get("error")
        if error_type:
            attributes["error.type"] = error_type
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(result)))

        span.set_attribute("total_retries", i)
        for key, value in attributes.items():
            span.set_attribute(key, value)

        duration = time.monotonic() - start_time
        _oauth_client_duration_histogram.record(duration, attributes)
        _oauth_client_retries_histogram.record(i, attributes)
        _oauth_client_total_counter.add(1, attributes)

        # TODO: consider returning retry after time so it can be used in response
        return result


# TODO: Test timeouts
# TODO: Add global retry budget / circuit breaker?
def _fetch(
    span: trace.Span,
    prepared: requests.PreparedRequest,
    timeout: float,
    endpoint: str,
) -> tuple[OAuthResponse, HTTPStatus | None, int]:
    # Make sure we always have at least a minimal timeout.
    timeout = max(1.0, min(current_settings.fetch.timeout, timeout))
    start_time = time.time()

    session = get_session()
    status = None

    try:
        # TODO: switch to a context for tracking time.
        resp = session.send(prepared, timeout=timeout)
    except requests.exceptions.RequestException as e:
        request_latency = time.time() - start_time

        span.record_exception(e)

        span.add_event("Closing session to get new server")
        session.close()

        # Fallback values in case we can't say anything better.
        status_label = "unknown_exception"
        description = "An unknown error occurred while talking to provider."

        # Don't give API users error messages we don't control the contents of.
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

        # TODO: Should this be temporarily_unavailable?

        # Server error isn't allowed everywhere, but fixing this has been
        # brought up in https://www.rfc-editor.org/errata_search.php?eid=4745
        result = OAuthError.SERVER_ERROR.json(description=description)

        if isinstance(e, requests.exceptions.HTTPError):
            length = len(e.response.content)
            retry_after = parse_retry(e.response.headers.get("retry-after"))
            # TODO: Can we decode the response here? Store it in the span?
        else:
            length = None
            retry_after = 0
    else:
        request_latency = time.time() - start_time

        result = _decode(span, resp)
        status = HTTPStatus(resp.status_code)
        status_label = stats.status(status)
        length = len(resp.content)
        retry_after = parse_retry(resp.headers.get("retry-after"))

    labels = {"endpoint": endpoint, "status": status_label}
    if length is not None:
        stats.ClientResponseSizeHistogram.labels(**labels).observe(length)
    stats.ClientLatencyHistogram.labels(**labels).observe(request_latency)

    return result, status, retry_after


def _decode(span: trace.Span, resp: requests.Response) -> OAuthResponse:
    # Per OAuth spec all responses should be JSON, but this isn't always
    # the case. For instance 502 errors and a gateway that does not correctly
    # create a fake JSON error response.

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


def redirect(uri: str, **params: str) -> flask.Response:
    # TODO: Add the location we redirect to in span?
    return flask.Response(
        status=HTTPStatus.FOUND,
        headers={"Location": rewrite_uri(uri, params)},
    )
