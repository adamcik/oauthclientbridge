import contextlib
import hashlib
import json
import sqlite3
import time
import urllib
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


def ouath_error(code, description=None):
    """Helper to serve oauth errors as JSON."""
    result = {'error': code}
    if description:
        result['error_description'] = description
    response = jsonify(result)

    if request.authorization:
        response.status_code = 401
        response.www_authenticate.set_basic()
    else:
        response.status_code = 400
    return response


@app.route('/')
def authorize():
    """Store random state in session cookie and redirect to auth endpoint."""
    # TODO: support setting extra params to auth redirect?
    session['state'] = str(uuid.uuid4())
    uri = app.config['OAUTH_AUTHORIZATION_URI'] + '?' + urllib.urlencode({
        'client_id': app.config['OAUTH_CLIENT_ID'],
        'response_type': 'code',
        'redirect_uri': app.config['OAUTH_REDIRECT_URI'],
        'scope': ' '.join(app.config['OAUTH_SCOPES']),
        'state': session['state'],
    })
    return redirect(uri)


@app.route('/callback')
def callback():
    """Validate callback and trade in code for a token."""
    if session.get('state', object()) != request.args.get('state'):
        return render(error='Invalid state.'), 400
    elif 'error' in request.args:
        return render(error=request.args['error']), 400

    auth = (app.config['OAUTH_CLIENT_ID'], app.config['OAUTH_CLIENT_SECRET'])
    result = requests.post(app.config['OAUTH_TOKEN_URI'], auth=auth, data={
        'grant_type': 'authorization_code',
        'redirect_uri': app.config['OAUTH_REDIRECT_URI'],
        'code': request.args.get('code'),
    }).json()

    del session['state']  # Delete the state in case of replay.

    if 'error' in result:
        return render(**result), 400

    client_id = str(uuid.uuid4())
    client_secret = fernet.Fernet.generate_key()
    token = encrypt(client_secret, json.dumps(result))

    with get_cursor() as cursor:
        cursor.execute('INSERT INTO tokens (client_id, token) VALUES (?, ?)',
                       (client_id, token))

    return render(client_id=client_id, client_secret=client_secret)


@app.route('/token', methods=['POST'])
def token():
    """Validate token request, refreshing when needed."""

    if request.form.get('grant_type') != 'client_credentials':
        return ouath_error('unsupported_grant_type',
                           'Only "client_credentials" is supported.')
    elif request.form.get('scope'):
        return ouath_error('invalid_scope', 'Setting scope is not supported.')
    elif request.authorization and request.authorization.type != 'basic':
        return ouath_error('invalid_request', 'Only Basic Auth is supported.')

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
        return ouath_error('invalid_request', 'Too many requests.')

    if not client_id or not client_secret:
        return ouath_error('invalid_client',
                           'Both client_id and client_secret must be set.')

    with get_cursor() as cursor:
        cursor.execute(
            'SELECT client_id, token FROM tokens WHERE client_id = ?',
            (client_id,))
        row = cursor.fetchone()

    if row is None:
        return ouath_error('invalid_client', 'Client not known.')
    elif row[1] is None:
        return ouath_error('invalid_grant', 'Grant has been revoked.')

    try:
        result = json.loads(decrypt(client_secret, row[1]))
    except fernet.InvalidToken:
        # Always return same message as for client not found to avoid leaking
        # valid clients directly, timing attacks could of course still work.
        return ouath_error('invalid_client', 'Client not known.')

    if 'refresh_token' not in result:
        return jsonify(result)

    auth = (app.config['OAUTH_CLIENT_ID'], app.config['OAUTH_CLIENT_SECRET'])
    refresh_result = requests.post(
        app.config['OAUTH_REFRESH_URI'], auth=auth, data={
            'grant_type': 'refresh_token',
            'refresh_token': result['refresh_token'],
        }).json()

    if 'error' in refresh_result:
        return ouath_error(
            refresh_result['error'], refresh_result.get('error_description'))

    result.update(refresh_result)

    with get_cursor() as cursor:
        cursor.execute('UPDATE tokens SET token = ? WHERE client_id = ?',
                       (encrypt(client_secret, json.dumps(result)), client_id))

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
