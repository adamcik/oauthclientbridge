import re
import time
import urllib
import urlparse

import requests

from requests.packages import urllib3

from flask import g, jsonify, redirect as flask_redirect, request

from oauthclientbridge import __version__, app, stats


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
    if e.uri is not None:
        result['error_uri'] = e.uri

    response = jsonify(result)
    if e.error == 'invalid_client':
        response.status_code = 401
        response.www_authenticate.set_basic()
    elif e.retry_after:
        response.headers['Retry-After'] = int(e.retry_after + 1)
        response.status_code = 429
    else:
        response.status_code = 400

    return response


def fallback_error_handler(e):
    result = {'error': 'server_error', 'error_description': 'Unhandled error.'}
    return jsonify(result), 500


def nocache(response):
    """Turns off caching in case there is sensitive content in responses."""
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    return response


def fetch(uri, username, password, **data):
    """Perform post given URI with auth and provided data."""
    req = requests.Request('POST', uri, auth=(username, password), data=data)
    prepared = req.prepare()

    timeout = time.time() + app.config['OAUTH_FETCH_TIMEOUT']
    retry = 0

    error_description = 'An unknown error occurred talking to provider.'
    result = {'error': 'server_error', 'error_description': error_description}

    for i in range(app.config['OAUTH_FETCH_TOTAL_RETRIES']):
        prefix = 'attempt #%d %s' % (i + 1, uri)

        backoff = (2**i - 1) * app.config['OAUTH_FETCH_BACKOFF_FACTOR']
        remaining_timeout = timeout - time.time()

        if (retry or backoff) > remaining_timeout:
            app.logger.debug('Abort %s no timeout remaining.', prefix)
            break
        elif (retry or backoff) > 0:
            app.logger.debug('Retry %s [sleep %.3f]', prefix, retry or backoff)
            time.sleep(retry or backoff)

        result, status, retry = _fetch(prepared, remaining_timeout)

        if status is not None and 'error' in result:
            status_enum = stats.status_enum(status)
            stats.ClientErrorCounter.labels(url=uri, status=status_enum,
                                            error=result['error']).inc()

        if status is None:
            pass  # We didn't even get a response, so try again.
        elif status not in app.config['OAUTH_FETCH_RETRY_STATUS_CODES']:
            break
        elif 'error' not in result:
            break  # No error reported so might as well return it.

        app.logger.debug('Result %s [status %s] [retry after %s]',
                         prefix, status, retry)

    return result


def _fetch(prepared, timeout):
    try:
        resp = _session().send(prepared, timeout=timeout)
    except IOError as e:
        # Don't give API users error messages we don't control the contents of.
        if isinstance(e, requests.exceptions.ConnectionError):
            description = 'An error occurred while connecting to the provider.'
        elif isinstance(e, requests.exceptions.Timeout):
            description = 'Request timed out while talking to provider.'
        else:
            description = 'An unknown error occurred talking to provider.'

        app.logger.warning('Fetching %r failed: %s', prepared.url, e)

        # Server error isn't allowed everywhere, but fixing this has been
        # brought up in https://www.rfc-editor.org/errata_search.php?eid=4745
        result = {'error': 'server_error', 'error_description': description}
        status_code = None
        retry_after = 0
    else:
        result = _decode(resp)
        status_code = resp.status_code
        retry_after = _parse_retry(resp.headers.get('retry-after'))

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
             resp.headers.get('Content-Type', 'text/plain'), e)

    description = 'Invalid JSON response from provider (%s)' % resp.status_code
    return {'error': 'server_error', 'error_description': description}


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
    scheme, netloc, path, query, fragment = urlparse.urlsplit(uri)
    query = _rewrite_query(query, params)
    return urlparse.urlunsplit((scheme, netloc, path, query, fragment))
