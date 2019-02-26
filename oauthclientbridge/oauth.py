import re
import time
import urllib
import urlparse

import requests

from requests.packages import urllib3

from flask import g, jsonify, redirect as flask_redirect, request

from oauthclientbridge import __version__, app, stats

ACCESS_DENIED = 'access_denied'
INVALID_CLIENT = 'invalid_client'
INVALID_GRANT = 'invalid_grant'
INVALID_REQUEST = 'invalid_request'
INVALID_SCOPE = 'invalid_scope'
INVALID_STATE = 'invalid_scope'
INVALID_RESPONSE = 'invalid_response'
SERVER_ERROR = 'server_error'
TEMPORARILY_UNAVAILABLE = 'temporarily_unavailable'
UNAUTHORIZED_CLIENT = 'unauthorized_client'
UNSUPPORTED_GRANT_TYPE = 'unsupported_grant_type'
UNSUPPORTED_RESPONSE_TYPE = 'unsupported_response_type'

# https://tools.ietf.org/html/rfc6749#section-4.1.2.1
AUTHORIZATION_ERRORS = {
    INVALID_REQUEST,
    UNAUTHORIZED_CLIENT,
    ACCESS_DENIED,
    UNSUPPORTED_RESPONSE_TYPE,
    INVALID_SCOPE,
    SERVER_ERROR,
    TEMPORARILY_UNAVAILABLE,
}

# https://tools.ietf.org/html/rfc6749#section-5.2
TOKEN_ERRORS = {
    INVALID_REQUEST,
    INVALID_CLIENT,
    INVALID_GRANT,
    UNAUTHORIZED_CLIENT,
    UNSUPPORTED_GRANT_TYPE,
    INVALID_SCOPE,
    # These are not really supported by RFC:
    SERVER_ERROR,
    TEMPORARILY_UNAVAILABLE,
}

ERROR_DESCRIPTIONS = {
    INVALID_REQUEST: (
        'The request is missing a required parameter, includes an invalid '
        'parameter value, includes a parameter more than once, or is '
        'otherwise malformed.'
    ),
    INVALID_CLIENT: (
        'Client authentication failed (e.g., unknown client, no client '
        'authentication included, or unsupported authentication method).'
    ),
    INVALID_GRANT: (
        'The provided authorization grant or refresh token is invalid, '
        'expired or revoked.'
    ),
    UNAUTHORIZED_CLIENT: (
        'The client is not authorized to perform this action.'
    ),
    ACCESS_DENIED: (
        'The resource owner or authorization server denied the request.'
    ),
    UNSUPPORTED_RESPONSE_TYPE: (
        'The authorization server does not support obtaining an authorization '
        'code using this method.'
    ),
    UNSUPPORTED_GRANT_TYPE: (
        'The authorization grant type is not supported by the authorization '
        'server.'
    ),
    INVALID_SCOPE: (
        'The requested scope is invalid, unknown, or malformed.'
    ),
    SERVER_ERROR: (
        'The server encountered an unexpected condition that prevented it '
        'from fulfilling the request.'
    ),
    TEMPORARILY_UNAVAILABLE: (
        'The server is currently unable to handle the request due to a '
        'temporary overloading or maintenance of the server.'
    ),
}


class Error(Exception):
    def __init__(self, error, description=None, uri=None, retry_after=None):
        self.error = error
        self.description = description
        self.uri = uri
        self.retry_after = retry_after


def error_handler(e):
    """Create a well formed JSON response with status and auth headers."""
    result = {'error': e.error}
    if e.description is not None:
        result['error_description'] = e.description
    elif e.error in ERROR_DESCRIPTIONS:
        result['error_description'] = ERROR_DESCRIPTIONS[e.error]
    if e.uri is not None:
        result['error_uri'] = e.uri

    response = jsonify(result)
    if e.error == INVALID_CLIENT:
        response.status_code = 401
        response.www_authenticate.set_basic()
    elif e.retry_after:
        response.headers['Retry-After'] = int(e.retry_after + 1)
        response.status_code = 429
    else:
        response.status_code = 400

    status = status=stats.status(response.status_code)
    stats.ServerErrorCounter.labels(
        endpoint=stats.endpoint(), status=status, error=e.error).inc()
    return response


def fallback_error_handler(e):
    stats.ServerErrorCounter.labels(
        endpoint=stats.endpoint(), status=stats.status(500),
        error=SERVER_ERROR).inc()

    return jsonify({
        'error': SERVER_ERROR,
        'error_description': ERROR_DESCRIPTIONS[SERVER_ERROR],
    }), 500


def nocache(response):
    """Turns off caching in case there is sensitive content in responses."""
    if 'Cache-Control' not in response.headers:
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Pragma'] = 'no-cache'
    return response


def normalize_error(error, error_types):
    """Translate any "bad" error types to something more usable."""
    error = app.config['OAUTH_FETCH_ERROR_TYPES'].get(error, error)

    if error not in error_types:
        return SERVER_ERROR
    else:
        return error


def validate_token(token):
    return token.get('access_token') and token.get('token_type')


def scrub_refresh_token(token):
    remove = ('access_token', 'expires_in', 'token_type')
    return {k: v for k, v in token.items() if k not in remove}


def fetch(uri, username, password, endpoint=None, **data):
    """Perform post given URI with auth and provided data."""
    req = requests.Request('POST', uri, auth=(username, password), data=data)
    prepared = req.prepare()

    timeout = time.time() + app.config['OAUTH_FETCH_TOTAL_TIMEOUT']
    retry = 0

    error_description = 'An unknown error occurred talking to provider.'
    result = {'error': SERVER_ERROR, 'error_description': error_description}

    for i in range(app.config['OAUTH_FETCH_TOTAL_RETRIES']):
        prefix = 'attempt #%d %s' % (i + 1, uri)

        # TODO: Add jitter to backoff and/or retry after?
        backoff = (2**i - 1) * app.config['OAUTH_FETCH_BACKOFF_FACTOR']
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
            if error not in ERROR_DESCRIPTIONS:
                error = 'invalid_error'
            stats.ClientErrorCounter.labels(error=error, **labels).inc()

        if status is None:
            pass  # We didn't even get a response, so try again.
        elif status not in app.config['OAUTH_FETCH_RETRY_STATUS_CODES']:
            break
        elif 'error' not in result:
            break  # No error reported so might as well return it.

        app.logger.debug('Result %s [status %s] [retry after %s]',
                         prefix, status, retry)

    # TODO: consider returning retry after time so it can be used.
    return result


def _fetch(prepared, timeout, endpoint):
    # Make sure we always have at least a minimal timeout.
    timeout = max(1.0, min(app.config['OAUTH_FETCH_TIMEOUT'], timeout))

    # TODO: switch to a context for the session? close on exception?
    s = _session()

    try:
        # TODO: switch to a context for tracking time.
        start_time = time.time()
        resp = s.send(prepared, timeout=timeout)
    except requests.exceptions.RequestException as e:
        request_latency = time.time() - start_time

        # Increase chances that we get connected to a different instance.
        s.close()

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
        result = {'error': SERVER_ERROR, 'error_description': description}
        status_code = None
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


def _decode(resp):
    # Per OAuth spec all responses should be JSON, but this isn't allways
    # the case. For instance 502 errors and a gateway that does not correctly
    # create a fake JSON error response.

    try:
        return resp.json()
    except ValueError as e:
        app.logger.warning(
            'Fetching %r (HTTP %s, %s) failed: %s', resp.url, resp.status_code,
             resp.headers.get('Content-Type', '-'), e)

    if resp.status_code in app.config['OAUTH_FETCH_UNAVAILABLE_STATUS_CODES']:
        error = TEMPORARILY_UNAVAILABLE
        description = 'Provider is unavailable.'
    else:
        error = SERVER_ERROR
        description = 'Unhandled provider error (HTTP %s).' % resp.status_code

    return {'error': error, 'error_description': description}


def _parse_retry(value):
    if not value:
        seconds = 0
    elif re.match(r'^\s*[0-9]+\s*$', value):
        seconds = int(value)
    else:
        date_tuple = email.utils.parsedate(value)
        if date_tuple is None:
            seconds = 0
        else:
            seconds = time.mktime(date_tuple) - time.time()
    return max(0, seconds)


def redirect(uri, **params):
    return flask_redirect(_rewrite_uri(uri, params))


def _session():
    if getattr(g, '_oauth_session', None) is None:
        g._oauth_session = requests.Session()
        g._oauth_session.headers['user-agent'] = (
            'oauthclientbridge %s' % __version__)
    return g._oauth_session


def _rewrite_query(original, params):
    # TODO: test this...
    parts = []
    query = urlparse.parse_qs(original, keep_blank_values=True)
    for key, value in params.items():
        query[key] = [value]  # Override with new params.
    for key, values in query.items():
        for value in values:  # Turn query into list of tuples.
            if isinstance(value, unicode):
                value = value.encode('utf-8')
            parts.append((key, value))
    return urllib.urlencode(parts)


def _rewrite_uri(uri, params):
    # TODO: test this and move to utils.py?
    scheme, netloc, path, query, fragment = urlparse.urlsplit(uri)
    query = _rewrite_query(query, params)
    return urlparse.urlunsplit((scheme, netloc, path, query, fragment))
