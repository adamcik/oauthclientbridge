import contextlib
import hashlib
import json
import sqlite3
import time
import urllib
import urlparse
import uuid

import click

from cryptography import fernet

from flask import (Flask, g, jsonify, redirect, render_template_string,
                   request, session)

import requests

app = Flask(__name__)
app.config.from_envvar('OAUTH_SETTINGS')


class OAuthError(Exception):
    def __init__(self, error, error_description=None, error_uri=None):
        self.error = error
        self.description = error_description
        self.uri = error_uri


def get_db():
    """Get singleton SQLite database connection."""
    if getattr(g, '_oauth_connection', None) is None:
        g._oauth_connection = sqlite3.connect(app.config['OAUTH_DATABASE'])
    return g._oauth_connection


@contextlib.contextmanager
def get_cursor():
    """Get SQLite cursor with automatic commit if no exceptions are raised."""
    with get_db() as connection:
        yield connection.cursor()


@app.teardown_appcontext
def close_db(exception):
    """Ensure that connection gets closed when app teardown happens."""
    c, g._oauth_connection = getattr(g, '_oauth_connection', None), None
    if c is not None:
        c.close()


@app.cli.command()
def initdb():
    """Initializes the database."""
    click.echo('Initializing %s' % app.config['OAUTH_DATABASE'])
    with app.open_resource('schema.sql', mode='r') as f:
        schema = f.read()
    with get_cursor() as cursor:
        cursor.executescript(schema)


@app.cli.command()
def cleandb():
    """Cleans database of stale data."""
    now = time.time()
    with get_cursor() as cursor:
        cursor.execute('DELETE FROM buckets WHERE updated < ? AND '
                       'value - (? - updated) / ? <= 0',
                       (now, now, app.config['OAUTH_BUCKET_REFILL_RATE']))
        click.echo('Deleted %s stale buckets' % cursor.rowcount)


def encrypt(key, data):
    f = fernet.Fernet(bytes(key))
    return f.encrypt(bytes(data))


def decrypt(key, token):
    f = fernet.Fernet(bytes(key))
    return f.decrypt(bytes(token))


def rate_limit(key):
    """Decide if the given key should be rate limited.

    Calls are allowed whenever the bucket is below capacity. Each hit fills the
    bucket by one. Buckets drain at a configurable rate, though refill only
    happens when the bucket gets a hit. There is a maximum bucket fill to avoid
    callers being locket out for too long.
    """
    now = time.time()
    key = hashlib.sha256(key).hexdigest()

    with get_cursor() as cursor:
        cursor.execute(
            'SELECT updated, value FROM buckets WHERE key = ?', (key,))
        row = cursor.fetchone()

        if row:
            updated, value = row
        else:
            updated, value = now, 0

        # TODO: add a penalty for being over cap?
        # TODO: this is probably racy.

        # 1. Reduce by amount we should have refilled since last update.
        value -= float(now - updated) / app.config['OAUTH_BUCKET_REFILL_RATE']
        # 2. Update to 0 if bucket is "full" or value + 1 to account for hit.
        value = max(0, value + 1)
        # 3. Limit how much over you can go.
        value = min(value, app.config['OAUTH_BUCKET_MAX_HITS'])

        cursor.execute(  # Insert/replace the bucket we just hit.
            'INSERT OR REPLACE INTO buckets '
            '(key, updated, value) VALUES (?, ?, ?)',
            (key, now, value))

    return value > app.config['OAUTH_BUCKET_CAPACITY']


@app.after_request
def nocache(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.errorhandler(OAuthError)
def oauth_error(e):
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


def render(client_id=None, client_secret=None, error=None):
    return render_template_string(
        app.config['OAUTH_CALLBACK_TEMPLATE'],
        client_id=client_id, client_secret=client_secret, error=error)


def update_query(original, params):
    """Parses the query parameters and updates them."""
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


def update_uri_params(uri, **params):
    """Parses the URI and updated the query parameters."""
    scheme, netloc, path, query, fragment = urlparse.urlsplit(uri)
    query = update_query(query, params)
    return urlparse.urlunsplit((scheme, netloc, path, query, fragment))


@app.route('/')
def authorize():
    """Store random state in session cookie and redirect to auth endpoint."""
    session['state'] = str(uuid.uuid4())
    return redirect(update_uri_params(
        app.config['OAUTH_AUTHORIZATION_URI'],
        client_id=app.config['OAUTH_CLIENT_ID'],
        response_type='code',
        redirect_uri=app.config['OAUTH_REDIRECT_URI'],
        scope=' '.join(app.config['OAUTH_SCOPES']),
        state=session['state']))


@app.route('/callback')
def callback():
    """Validate callback and trade in code for a token."""
    if session.get('state', object()) != request.args.get('state'):
        return render(error='Bad callback, state did not match.'), 400
    elif 'error' in request.args:
        return render(error=request.args['error']), 400

    uri = app.config['OAUTH_TOKEN_URI']
    auth = (app.config['OAUTH_CLIENT_ID'], app.config['OAUTH_CLIENT_SECRET'])
    data = {'grant_type': 'authorization_code',
            'redirect_uri': app.config['OAUTH_REDIRECT_URI'],
            'code': request.args.get('code')}

    try:
        response = requests.post(uri, auth=auth, data=data)
        response.raise_for_status()
        result = response.json()
    except requests.exceptions.RequestException as e:
        app.logger.error('Token fetch failed: %s', e)
        return render(error='Token fetch failed.'), 500

    del session['state']  # Delete the state in case of replay.

    if 'error' in result:
        return render(error=result['error']), 400

    client_id = str(uuid.uuid4())
    client_secret = fernet.Fernet.generate_key()
    token = encrypt(client_secret, json.dumps(result))

    with get_cursor() as cursor:
        try:
            cursor.execute(
                'INSERT INTO tokens (client_id, token) VALUES (?, ?)',
                (client_id, token))
        except sqlite3.IntegrityError:
            return render(error='Could not get unique client id.'), 500

    return render(client_id=client_id, client_secret=client_secret)


@app.route('/token', methods=['POST'])
def token():
    """Validate token request, refreshing when needed."""

    if request.form.get('grant_type') != 'client_credentials':
        raise OAuthError('unsupported_grant_type',
                         'Only "client_credentials" is supported.')
    elif request.form.get('scope'):
        raise OAuthError('invalid_scope', 'Setting scope is not supported.')
    elif request.authorization and request.authorization.type != 'basic':
        raise OAuthError('invalid_client', 'Only Basic Auth is supported.')

    client_id = request.form.get('client_id')
    client_secret = request.form.get('client_secret')
    if (client_id or client_secret) and request.authorization:
        raise OAuthError('invalid_request',
                         'More than one mechanism for authenticating set.')
    elif request.authorization:
        client_id = request.authorization.username
        client_secret = request.authorization.password

    client_limit = rate_limit(client_id)
    addr_limit = rate_limit(request.remote_addr)
    if client_limit or addr_limit:
        app.logger.warning('Rate limiting: client_id=%s address=%s',
                           client_limit, addr_limit)
        raise OAuthError('invalid_request', 'Too many requests.')

    if not client_id or not client_secret:
        raise OAuthError('invalid_client',
                         'Both client_id and client_secret must be set.')

    with get_cursor() as cursor:
        cursor.execute(
            'SELECT token FROM tokens WHERE client_id = ?', (client_id,))
        row = cursor.fetchone()

    if row is None:
        raise OAuthError('invalid_client', 'Client not known.')
    elif row[0] is None:
        raise OAuthError('invalid_grant', 'Grant has been revoked.')

    try:
        result = json.loads(decrypt(client_secret, row[0]))
    except fernet.InvalidToken:
        # Always return same message as for client not found to avoid leaking
        # valid clients directly, timing attacks could of course still work.
        raise OAuthError('invalid_client', 'Client not known.')

    if 'refresh_token' not in result:
        return jsonify(result)

    uri = app.config['OAUTH_REFRESH_URI']
    auth = (app.config['OAUTH_CLIENT_ID'], app.config['OAUTH_CLIENT_SECRET'])
    data = {'grant_type': 'refresh_token',
            'refresh_token': result['refresh_token']}

    try:
        response = requests.post(uri, auth=auth, data=data)
        response.raise_for_status()
        refresh_result = response.json()
    except requests.exceptions.RequestException as e:
        app.logger.error('Token refresh failed: %s', e)
        # Server error isn't currently allowed, but fixing this has been
        # brought up in https://www.rfc-editor.org/errata_search.php?eid=4745
        raise OAuthError('server_error', 'Token refresh failed.')

    if 'error' in refresh_result:
        # Client Credentials access token responses use the same errors
        # as Authorization Code Grant access token responses. As such just
        # raise the error we got.
        raise OAuthError(refresh_result['error'],
                         refresh_result.get('error_description'),
                         refresh_result.get('error_uri'))

    result.update(refresh_result)
    token = encrypt(client_secret, json.dumps(result))

    with get_cursor() as cursor:
        cursor.execute('UPDATE tokens SET token = ? WHERE client_id = ?',
                       (token, client_id))

    del result['refresh_token']
    return jsonify(result)


@app.route('/revoke', methods=['POST'])
def revoke():
    """Sets the clients token to null."""
    client_id = request.form.get('client_id')
    if not client_id:
        return render(error='Missing client_id.'), 400
    with get_cursor() as cursor:
        cursor.execute(
            'UPDATE tokens SET token = null WHERE client_id = ?', (client_id,))
    # We always report success as to not leak info.
    return render(error='Revoked client_id.'), 200


if __name__ == '__main__':
    app.run()
