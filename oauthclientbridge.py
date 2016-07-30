import contextlib
import hashlib
import json
import sqlite3
import time
import urllib
import urlparse
import uuid

from cryptography import fernet

from flask import (
    jsonify, g, redirect, render_template_string, request, session, Flask)

import requests

app = Flask(__name__)
app.config.from_envvar('OAUTH_SETTINGS')


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


def init_db():
    """Runs schema.sql in the configured database."""
    with app.app_context():
        with app.open_resource('schema.sql', mode='r') as f:
            schema = f.read()
        with get_cursor() as cursor:
            cursor.executescript(schema)


def encrypt(key, data):
    f = fernet.Fernet(bytes(key))
    return f.encrypt(bytes(data))


def decrypt(key, token):
    f = fernet.Fernet(bytes(key))
    return f.decrypt(bytes(token))


def rate_limit(key):
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
        value -= float(now - updated) / app.config['OAUTH_BUCKET_REFILL_RATE']
        value = max(0, value + 1)
        value = min(value, app.config['OAUTH_BUCKET_MAX_HITS'])

        cursor.execute(  # Insert/replace the bucket we just hit.
            'INSERT OR REPLACE INTO buckets '
            '(key, updated, value) VALUES (?, ?, ?)',
            (key, now, value))

    return value > app.config['OAUTH_BUCKET_CAPACITY']


# TODO: integrate cleaning of stale limits along the lines of the following
def clear_stale_limits():
    now = time.time()
    with get_cursor() as cursor:
        cursor.execute('DELETE FROM buckets WHERE updated < ? AND '
                       'value - (? - updated) / ? <= 0',
                       (now, now, app.config['OAUTH_BUCKET_REFILL_RATE']))


def render(**context):
    return render_template_string(
        app.config['OAUTH_CALLBACK_TEMPLATE'], **context)


def oauth_error(code, description=None, uri=None):
    """Helper to serve oauth errors as JSON."""
    result = {'error': code}
    if description:
        result['error_description'] = description
    if uri:
        result['error_uri'] = uri

    response = jsonify(result)

    if request.authorization:
        response.status_code = 401
        response.www_authenticate.set_basic()
    else:
        response.status_code = 400
    return response


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
        return render(error='Invalid state.'), 400
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
        return render(error='Fetching OAuth token failed: %s' % str(e)), 500

    del session['state']  # Delete the state in case of replay.

    if 'error' in result:
        response = render(error=result['error'],
                          error_description=result.get('error_description'),
                          error_uri=result.get('error_uri'))
        return response, 400

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
        return oauth_error('unsupported_grant_type',
                           'Only "client_credentials" is supported.')
    elif request.form.get('scope'):
        return oauth_error('invalid_scope', 'Setting scope is not supported.')
    elif request.authorization and request.authorization.type != 'basic':
        return oauth_error('invalid_request', 'Only Basic Auth is supported.')

    if request.authorization:
        # TODO: test this flow.
        client_id = request.authorization.username
        client_secret = request.authorization.password
    else:
        client_id = request.form.get('client_id')
        client_secret = request.form.get('client_secret')

    client_limit = rate_limit(client_id)
    addr_limit = rate_limit(request.remote_addr)
    if client_limit or addr_limit:
        return oauth_error('invalid_request', 'Too many requests.')

    if not client_id or not client_secret:
        return oauth_error('invalid_client',
                           'Both client_id and client_secret must be set.')

    with get_cursor() as cursor:
        cursor.execute(
            'SELECT client_id, token FROM tokens WHERE client_id = ?',
            (client_id,))
        row = cursor.fetchone()

    if row is None:
        return oauth_error('invalid_client', 'Client not known.')
    elif row[1] is None:
        return oauth_error('invalid_grant', 'Grant has been revoked.')

    try:
        result = json.loads(decrypt(client_secret, row[1]))
    except fernet.InvalidToken:
        # Always return same message as for client not found to avoid leaking
        # valid clients directly, timing attacks could of course still work.
        return oauth_error('invalid_client', 'Client not known.')

    if 'refresh_token' not in result:
        return jsonify(result)

    auth = (app.config['OAUTH_CLIENT_ID'], app.config['OAUTH_CLIENT_SECRET'])
    refresh_result = requests.post(
        app.config['OAUTH_REFRESH_URI'], auth=auth, data={
            'grant_type': 'refresh_token',
            'refresh_token': result['refresh_token'],
        }).json()

    if 'error' in refresh_result:
        return oauth_error(refresh_result['error'],
                           description=refresh_result.get('error_description'),
                           uri=refresh_result.get('error_uri'))

    result.update(refresh_result)
    token = encrypt(client_secret, json.dumps(result))
    del result['refresh_token']

    with get_cursor() as cursor:
        cursor.execute('UPDATE tokens SET token = ? WHERE client_id = ?',
                       (token, client_id))

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
