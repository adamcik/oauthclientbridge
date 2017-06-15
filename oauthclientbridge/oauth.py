import urllib
import urlparse

import requests

from requests.packages import urllib3

from flask import g, jsonify, redirect as flask_redirect, request

from oauthclientbridge import app


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
    if request.authorization:
        response.status_code = 401
        response.www_authenticate.set_basic()
    else:
        response.status_code = 400

    if e.retry_after:
        response.headers['Retry-After'] = int(e.retry_after + 1)

    return response


def nocache(response):
    """Turns off caching in case there is sensitive content in responses."""
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    return response


def fetch(uri, username, password, **data):
    """Perform post given URI with auth and provided data."""
    try:
        resp = _session().post(uri, auth=(username, password), data=data,
                               timeout=app.config['OAUTH_FETCH_TIMEOUT'])
    except IOError as e:
        # Don't give API users error messages we don't control the contents of.
        if isinstance(e, requests.exceptions.ConnectionError):
            description = 'An error occurred while connecting to the provider.'
        elif isinstance(e, requests.exceptions.RetryError):
            description = 'Request exceeded allowed retries to provider.'
        elif isinstance(e, requests.exceptions.Timeout):
            description = 'Request timed out while talking to provider.'
        else:
            description = 'An unknown error occurred talking to provider.'

        app.logger.warning('Fetching %r failed: %s', uri, e)
        # Server error isn't allowed everywhere, but fixing this has been
        # brought up in https://www.rfc-editor.org/errata_search.php?eid=4745
        return {'error': 'server_error', 'error_description': description}

    try:
        result = resp.json()
    except ValueError as e:
        app.logger.warning('Fetching %r failed: %s', uri, e)
        app.logger.debug('Response: %s', _sanitize(resp.text))
        description = 'Decoding JSON response from provider failed.'
        return {'error': 'server_error', 'error_description': description}

    if 400 <= resp.status_code < 500 and 'error' not in result:
        status = httplib.responses.get(resp.status_code, resp.status_code)
        description = 'Got HTTP %s without error from provider.' % status
        app.logger.warning('Fetching %r failed: %s', uri, description)
        return {'error': 'server_error', 'error_description': description}

    # TODO: Log != 200 responses that make it here?
    return result


def redirect(uri, **params):
    return flask_redirect(_rewrite_uri(uri, params))


def _sanitize(value, cutoff=100):
    length = len(value)
    if length > cutoff:
        value = '%s...' % value[:cutoff]
    return '%r %d bytes' % (value, length)
    return value.encode('unicode-escape')


def _session():
    if getattr(g, '_oauth_session', None) is None:
        retry = urllib3.util.Retry(
            total=app.config['OAUTH_FETCH_TOTAL_RETRIES'],
            status_forcelist=app.config['OAUTH_FETCH_RETRY_STATUS_CODES'],
            backoff_factor=app.config['OAUTH_FETCH_BACKOFF_FACTOR'],
            method_whitelist=['POST'], respect_retry_after_header=True)

        adapter = requests.adapters.HTTPAdapter(max_retries=retry)

        g._oauth_session = requests.Session()
        g._oauth_session.mount('http://', adapter)
        g._oauth_session.mount('https://', adapter)

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
