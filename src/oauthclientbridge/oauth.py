import email.utils
import functools
import importlib.metadata
import re
import time
from typing import Any

import flask
import requests
import structlog
from opentelemetry import trace

from oauthclientbridge import errors, stats
from oauthclientbridge.settings import current_settings
from oauthclientbridge.utils import rewrite_uri

logger: structlog.BoundLogger = structlog.get_logger()
tracer = trace.get_tracer(__name__)

OAuthResponse = dict[str, Any]
URIParam = dict[str, str]

# https://tools.ietf.org/html/rfc6749#section-4.1.2.1
AUTHORIZATION_ERRORS = {
    errors.INVALID_REQUEST,
    errors.UNAUTHORIZED_CLIENT,
    errors.ACCESS_DENIED,
    errors.UNSUPPORTED_RESPONSE_TYPE,
    errors.INVALID_SCOPE,
    errors.SERVER_ERROR,
    errors.TEMPORARILY_UNAVAILABLE,
}

# https://tools.ietf.org/html/rfc6749#section-5.2
TOKEN_ERRORS = {
    errors.INVALID_REQUEST,
    errors.INVALID_CLIENT,
    errors.INVALID_GRANT,
    errors.UNAUTHORIZED_CLIENT,
    errors.UNSUPPORTED_GRANT_TYPE,
    errors.INVALID_SCOPE,
    # These are not really supported by RFC:
    errors.SERVER_ERROR,
    errors.TEMPORARILY_UNAVAILABLE,
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
        error: str,
        description: str | None = None,
        uri: str | None = None,
        retry_after: int | None = None,
    ):
        super().__init__()
        self.error = error
        self.description = description
        self.uri = uri
        self.retry_after = retry_after

    def __str__(self):
        return f"{self.error}: {self.description or '-'}"


def error_handler(e: Error) -> flask.Response:
    """Create a well formed JSON response with status and auth headers."""
    result = {"error": e.error}
    if e.description is not None:
        result["error_description"] = e.description
    elif e.error in errors.DESCRIPTIONS:
        result["error_description"] = errors.DESCRIPTIONS[e.error]
    if e.uri is not None:
        result["error_uri"] = e.uri

    response = flask.jsonify(result)
    if e.error == errors.INVALID_CLIENT:
        response.status_code = 401
        # TODO: This triggers a login prompt when testing, we probably don't want that.
        response.headers["WWW-Authenticate"] = (
            f'Basic realm="{current_settings.auth_realm}"'
        )
    elif e.retry_after:
        response.headers["Retry-After"] = int(e.retry_after + 1)
        response.status_code = 429
    else:
        response.status_code = 400

    current_span = trace.get_current_span()
    current_span.record_exception(e)
    current_span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))

    status = status = stats.status(response.status_code)
    stats.ServerErrorCounter.labels(
        endpoint=stats.endpoint(), status=status, error=e.error
    ).inc()
    return response


def fallback_error_handler(e: Exception) -> flask.Response:
    stats.ServerErrorCounter.labels(
        endpoint=stats.endpoint(),
        status=stats.status(500),
        error=errors.SERVER_ERROR,
    ).inc()

    current_span = trace.get_current_span()
    current_span.record_exception(e)
    current_span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))

    response = flask.jsonify(
        _error(errors.SERVER_ERROR, errors.DESCRIPTIONS[errors.SERVER_ERROR])
    )
    response.status_code = 500
    return response


def nocache(response: flask.Response) -> flask.Response:
    """Turns off caching in case there is sensitive content in responses."""
    if "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


def normalize_error(error: str, error_types: set[str]) -> str:
    """Translate any "bad" error types to something more usable."""
    error = current_settings.fetch.error_types.get(error, error)
    if error not in error_types:
        return errors.SERVER_ERROR
    else:
        return error


def validate_token(token: OAuthResponse) -> bool:
    return bool(token.get("access_token") and token.get("token_type"))


def scrub_refresh_token(token: OAuthResponse) -> OAuthResponse:
    remove = ("access_token", "expires_in", "token_type")
    return {k: v for k, v in token.items() if k not in remove}


# TODO: Turn endpoint into a StrEnum
def fetch(uri: str, endpoint: str, auth: str | None = None, **data) -> OAuthResponse:
    """Perform post given URI with auth and provided data."""

    with tracer.start_as_current_span(f"OAUTH {endpoint}") as span:
        req = requests.Request("POST", uri, data=data, auth=auth)
        prepared = req.prepare()

        timeout = time.time() + current_settings.fetch.total_timeout
        retry = 0

        result = _error(
            errors.SERVER_ERROR, "An unknown error occurred talking to provider."
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

            labels = {"endpoint": endpoint, "status": stats.status(status)}
            stats.ClientRetryHistogram.labels(**labels).observe(i)

            if status is not None and "error" in result:
                error = result["error"]
                error = current_settings.fetch.error_types.get(error, error)
                if error not in errors.DESCRIPTIONS:
                    error = "invalid_error"
                stats.ClientErrorCounter.labels(error=error, **labels).inc()

            if status is None:
                span.add_event("Missing response")
                pass  # We didn't even get a response, so try again.
            elif status == 200:
                span.add_event("Success")
                break
            elif status not in current_settings.fetch.retry_status_codes:
                span.add_event("Aborted", {"status": status})
                break
            elif "error" not in result:
                span.add_event("Non-OK without error!?")
                break  # No error reported so might as well return it.

            logger.debug(
                "Result %s [status %s] [retry after %s]", prefix, status, retry
            )

        span.set_attribute("total_retries", i)

        if "error" in result:
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(result)))

        # TODO: consider returning retry after time so it can be used in response
        return result


# TODO: Test timeouts
# TODO: Add global retry budget / circuit breaker?
def _fetch(
    span: trace.Span,
    prepared: requests.PreparedRequest,
    timeout: float,
    endpoint: str,
) -> tuple[OAuthResponse, int, int]:
    # Make sure we always have at least a minimal timeout.
    timeout = max(1.0, min(current_settings.fetch.timeout, timeout))
    start_time = time.time()

    session = get_session()

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

        logger.warning("Fetching %r failed: %s", prepared.url, e)

        # TODO: Should this be temporarily_unavailable?

        # Server error isn't allowed everywhere, but fixing this has been
        # brought up in https://www.rfc-editor.org/errata_search.php?eid=4745
        result = _error(errors.SERVER_ERROR, description)
        status_code = 504

        if isinstance(e, requests.exceptions.HTTPError):
            length = len(e.response.content)
            retry_after = parse_retry(e.response.headers.get("retry-after"))
            # TODO: Can we decode the response here? Store it in the span?
        else:
            length = None
            retry_after = 0
    else:
        request_latency = time.time() - start_time
        status_label = stats.status(resp.status_code)

        result = _decode(span, resp)
        status_code = resp.status_code
        length = len(resp.content)
        retry_after = parse_retry(resp.headers.get("retry-after"))

    labels = {"endpoint": endpoint, "status": status_label}
    if length is not None:
        stats.ClientResponseSizeHistogram.labels(**labels).observe(length)
    stats.ClientLatencyHistogram.labels(**labels).observe(request_latency)

    return result, status_code, retry_after


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
        error = errors.TEMPORARILY_UNAVAILABLE
        description = "Provider is unavailable."
    else:
        error = errors.SERVER_ERROR
        description = "Unhandled provider error (HTTP %s)." % resp.status_code

    return _error(error, description)


def _error(error: str, description: str) -> OAuthResponse:
    return {"error": error, "error_description": description}


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

    # TODO: Add the location we redirect to in span?
    return flask.Response(status=302, headers={"Location": rewrite_uri(uri, params)})
