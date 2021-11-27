import email.utils
import re
import time
import typing

import flask
import requests

from oauthclientbridge import __version__, app, compat, errors, stats

if typing.TYPE_CHECKING:
    from typing import Any, Dict, Optional, Set, Tuple, Text  # noqa: F401


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

_session = requests.Session()
_session.headers['user-agent'] = 'oauthclientbridge %s' % __version__


class Error(Exception):
    def __init__(self, error, description=None, uri=None, retry_after=None):
        # type: (Text, Optional[Text], Optional[Text], Optional[int]) -> None
        self.error = error
        self.description = description
        self.uri = uri
        self.retry_after = retry_after


def error_handler(e):  # type: (Error) -> flask.Response
    """Create a well formed JSON response with status and auth headers."""
    result = {'error': e.error}
    if e.description is not None:
        result['error_description'] = e.description
    elif e.error in errors.DESCRIPTIONS:
        result['error_description'] = errors.DESCRIPTIONS[e.error]
    if e.uri is not None:
        result['error_uri'] = e.uri

    response = flask.jsonify(result)  # type: flask.Response
    if e.error == errors.INVALID_CLIENT:
        response.status_code = 401
        response.www_authenticate.set_basic()
    elif e.retry_after:
        response.headers['Retry-After'] = int(e.retry_after + 1)
        response.status_code = 429
    else:
        response.status_code = 400

    status = status = stats.status(response.status_code)
    stats.ServerErrorCounter.labels(
        endpoint=stats.endpoint(), status=status, error=e.error
    ).inc()
    return response


def fallback_error_handler(e):  # type: (Exception) -> flask.Response
    stats.ServerErrorCounter.labels(
        endpoint=stats.endpoint(),
        status=stats.status(500),
        error=errors.SERVER_ERROR,
    ).inc()

    response = flask.jsonify(
        _error(errors.SERVER_ERROR, errors.DESCRIPTIONS[errors.SERVER_ERROR])
    )  # type: flask.Response
    response.status_code = 500
    return response


def nocache(response):  # type: (flask.Response) -> flask.Response
    """Turns off caching in case there is sensitive content in responses."""
    if 'Cache-Control' not in response.headers:
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Pragma'] = 'no-cache'
    return response


def normalize_error(error, error_types):  # type: (Text, Set[str]) -> Text
    """Translate any "bad" error types to something more usable."""
    error = app.config['OAUTH_FETCH_ERROR_TYPES'].get(error, error)
    if error not in error_types:
        return errors.SERVER_ERROR
    else:
        return error


def validate_token(token):  # type: (Dict[Text, Any]) -> bool
    return bool(token.get('access_token') and token.get('token_type'))


def scrub_refresh_token(token):  # type: (Dict[Text, Any]) -> Dict[Text, Any]
    remove = ('access_token', 'expires_in', 'token_type')
    return {k: v for k, v in token.items() if k not in remove}


def fetch(uri, auth=None, endpoint=None, **data):
    # type: (Text, Text, Text, Optional[Text], **Any) -> Dict[Text, Any]
    """Perform post given URI with auth and provided data."""
    req = requests.Request('POST', uri, data=data, auth=auth)
    prepared = req.prepare()  # type: requests.PreparedRequest

    timeout = time.time() + app.config['OAUTH_FETCH_TOTAL_TIMEOUT']
    retry = 0

    result = _error(
        errors.SERVER_ERROR, 'An unknown error occurred talking to provider.'
    )

    for i in range(app.config['OAUTH_FETCH_TOTAL_RETRIES']):
        prefix = 'attempt #%d %s' % (i + 1, uri)

        # TODO: Add jitter to backoff and/or retry after?
        backoff = (2 ** i - 1) * app.config['OAUTH_FETCH_BACKOFF_FACTOR']
        remaining_timeout = timeout - time.time()

        if (retry or backoff) > remaining_timeout:
            app.logger.debug('Abort %s no timeout remaining.', prefix)
            break
        elif (retry or backoff) > 0:
            app.logger.debug('Retry %s [sleep %.3f]', prefix, retry or backoff)
            time.sleep(retry or backoff)

        result, status, retry = _fetch(prepared, remaining_timeout, endpoint)

        labels = {'endpoint': endpoint, 'status': stats.status(status)}
        stats.ClientRetryHistogram.labels(**labels).observe(i)

        if status is not None and 'error' in result:
            error = result['error']
            error = app.config['OAUTH_FETCH_ERROR_TYPES'].get(error, error)
            if error not in errors.DESCRIPTIONS:
                error = 'invalid_error'
            stats.ClientErrorCounter.labels(error=error, **labels).inc()

        if status is None:
            pass  # We didn't even get a response, so try again.
        elif status not in app.config['OAUTH_FETCH_RETRY_STATUS_CODES']:
            break
        elif 'error' not in result:
            break  # No error reported so might as well return it.

        app.logger.debug(
            'Result %s [status %s] [retry after %s]', prefix, status, retry
        )

    # TODO: consider returning retry after time so it can be used.
    return result


def _fetch(
    prepared,  # type: requests.PreparedRequest
    timeout,  # type: float
    endpoint=None,  # type: Optional[Text]
):  # type: (...) -> Tuple[Dict[Text, Any], int, int]

    # Make sure we always have at least a minimal timeout.
    timeout = max(1.0, min(app.config['OAUTH_FETCH_TIMEOUT'], timeout))
    start_time = time.time()

    try:
        # TODO: switch to a context for tracking time.
        resp = _session.send(prepared, timeout=timeout)
    except requests.exceptions.RequestException as e:
        request_latency = time.time() - start_time

        # Increase chances that we get connected to a different instance.
        _session.close()

        # Fallback values in case we can't say anything better.
        status_label = 'unknown_exception'
        description = 'An unknown error occurred while talking to provider.'

        # Don't give API users error messages we don't control the contents of.
        if isinstance(e, requests.exceptions.Timeout):
            description = 'Request timed out while connecting to provider.'
            if isinstance(e, requests.exceptions.ConnectTimeout):
                status_label = 'connection_timeout'
            elif isinstance(e, requests.exceptions.ReadTimeout):
                status_label = 'read_timeout'
        elif isinstance(e, requests.exceptions.ConnectionError):
            description = 'An error occurred while connecting to the provider.'
            if isinstance(e, requests.exceptions.SSLError):
                status_label = 'ssl_error'
            elif isinstance(e, requests.exceptions.ProxyError):
                status_label = 'proxy_error'
            else:
                status_label = 'connection_error'

        app.logger.warning('Fetching %r failed: %s', prepared.url, e)

        # TODO: Should this be temporarily_unavailable?

        # Server error isn't allowed everywhere, but fixing this has been
        # brought up in https://www.rfc-editor.org/errata_search.php?eid=4745
        result = _error(errors.SERVER_ERROR, description)
        status_code = 504
        length = None
        retry_after = 0
    else:
        request_latency = time.time() - start_time
        status_label = stats.status(resp.status_code)

        result = _decode(resp)
        status_code = resp.status_code
        length = len(resp.content)
        retry_after = _parse_retry(resp.headers.get('retry-after'))

    labels = {'endpoint': endpoint, 'status': status_label}
    if length is not None:
        stats.ClientResponseSizeHistogram.labels(**labels).observe(length)
    stats.ClientLatencyHistogram.labels(**labels).observe(request_latency)

    return result, status_code, retry_after


def _decode(resp):  # type: (requests.Response) -> Dict[Text, Any]
    # Per OAuth spec all responses should be JSON, but this isn't allways
    # the case. For instance 502 errors and a gateway that does not correctly
    # create a fake JSON error response.

    try:
        return resp.json()
    except ValueError as e:
        app.logger.warning(
            'Fetching %r (HTTP %s, %s) failed: %s',
            resp.url,
            resp.status_code,
            resp.headers.get('Content-Type', '-'),
            e,
        )

    if resp.status_code in app.config['OAUTH_FETCH_UNAVAILABLE_STATUS_CODES']:
        error = errors.TEMPORARILY_UNAVAILABLE
        description = 'Provider is unavailable.'
    else:
        error = errors.SERVER_ERROR
        description = 'Unhandled provider error (HTTP %s).' % resp.status_code

    return _error(error, description)


def _error(error, description):  # type: (Text, Text) -> Dict[Text, Any]
    return {'error': error, 'error_description': description}


def _parse_retry(value):  # type: (Text) -> int
    if not value:
        seconds = 0
    elif re.match(r'^\s*[0-9]+\s*$', value):
        seconds = int(value)
    else:
        date_tuple = email.utils.parsedate(value)
        if date_tuple is None:
            seconds = 0
        else:
            seconds = int(time.mktime(date_tuple) - time.time())
    return max(0, seconds)


def redirect(uri, **params):  # type: (Text, **Text) -> flask.Response
    return flask.Response(
        status=302, headers={'Location': _rewrite_uri(uri, params)}
    )


def _rewrite_query(original, params):
    # type: (Text, Dict[str, Text]) -> str
    # TODO: test this...
    parts = []
    query = compat.parse_qs(original, keep_blank_values=True)
    for p, value in params.items():
        query[p] = [value]  # Override with new params.
    for q, values in query.items():
        for value in values:  # Turn query into list of tuples.
            # TODO: params is really TEXT then this is no longer needed.
            if isinstance(value, compat.text_type):
                parts.append((q, value.encode('utf-8')))
            else:
                parts.append((q, value))
    return compat.urlencode(parts)


def _rewrite_uri(uri, params):  # type: (Text, Dict[str, Text]) -> Text
    # TODO: test this and move to utils.py?
    scheme, netloc, path, query, fragment = compat.urlsplit(uri)
    query = _rewrite_query(query, params)
    return compat.urlunsplit((scheme, netloc, path, query, fragment))
