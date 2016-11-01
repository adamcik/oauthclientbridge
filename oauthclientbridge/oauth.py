import urllib
import urlparse

import requests

from flask import jsonify, redirect as flask_redirect, request

from oauthclientbridge import app


class Error(Exception):
    def __init__(self, error, error_description=None, error_uri=None):
        self.error = error
        self.description = error_description
        self.uri = error_uri


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
    return response


def nocache(response):
    """Turns off caching in case there is sensitive content in responses."""
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    return response


def fetch(uri, username, password, **data):
    """Perform post given URI with auth and provided data."""
    response = requests.post(uri, auth=(username, password), data=data)
    status_code = response.status_code

    try:
        result = response.json()
    except ValueError:
        app.logger.warning('Fetching %r failed: Invalid JSON.')
        return {'error': 'server_error',
                'error_description': 'Decoding JSON response failed.'}

    if 400 <= status_code < 500 and 'error' not in result:
        status = httplib.responses.get(status_code, status_code)
        description = 'Got HTTP %s, but no error set in response.' % status
        app.logger.warning('Fetching %r failed: %s', uri, description())
        return {'error': 'server_error', 'error_description': description}

    # TODO: Log != 200 responses that make it here?

    return result


def redirect(uri, **params):
    return flask_redirect(_rewrite_uri(uri, params))


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
