import contextlib
import sqlite3
import re
import uuid

from flask import g

from oauthclientbridge import app, stats

Error = sqlite3.Error
IntegrityError = sqlite3.IntegrityError


def generate_id():
    return str(uuid.uuid4())


def initialize():
    with app.open_resource('schema.sql', mode='r') as f:
        schema = f.read()
    with get() as c:
        c.executescript(schema)


def get():
    """Get singleton SQLite database connection."""
    if getattr(g, '_oauth_database', None) is None:
        with stats.DBLatencyHistorgram.labels(query='connect').time():
            g._oauth_database = sqlite3.connect(
                app.config['OAUTH_DATABASE'],
                timeout=app.config['OAUTH_DATABASE_TIMEOUT'],
                isolation_level=None)
        if app.config['OAUTH_DATABASE_PRAGMA']:
            with stats.DBLatencyHistorgram.labels(query='pragma').time():
                g._oauth_database.execute(app.config['OAUTH_DATABASE_PRAGMA'])
    return g._oauth_database


def vacuum():
    with get() as c:
        c.execute('VACUUM')


@contextlib.contextmanager
def cursor(name, transaction=False):
    """Get SQLite cursor with automatic commit if no exceptions are raised."""
    try:
        with get() as connection:
            with stats.DBLatencyHistorgram.labels(query='cursor').time():
                c = connection.cursor()
            with contextlib.closing(c):
                with stats.DBLatencyHistorgram.labels(query=name).time():
                    try:
                        if transaction:
                            c.execute('BEGIN')
                        yield c
                    except:
                        if transaction:
                            c.execute('ROLLBACK')
                        raise
                    else:
                        if transaction:
                            c.execute('COMMIT')
    except sqlite3.Error as e:
        # https://www.python.org/dev/peps/pep-0249/#exceptions for values.
        error = re.sub(r'(?!^)([A-Z])', r'_\1', e.__class__.__name__).lower()
        stats.DBErrorCounter.labels(query=name, error=error).inc()
        raise


def insert(token):
    """Store encrypted token and return what client_id it was stored under."""
    client_id = generate_id()

    with cursor(name='insert_token', transaction=True) as c:
        # TODO: Retry creating client_id if it already exists?
        c.execute('INSERT INTO tokens (client_id, token) VALUES (?, ?)',
                  (client_id, token))
    return client_id


def lookup(client_id):
    """Lookup a client_id and return encrypted token.

    Raises a LookupError if client_id is not found.
    Returns the encrypted token or None if token is revoked.
    """
    with cursor(name='lookup_token') as c:
        c.execute('SELECT token FROM tokens WHERE client_id = ?', (client_id,))
        row = c.fetchone()

    if row is None:
        raise LookupError('Client not found.')
    else:
        return row[0]


def update(client_id, token):
    """Update a client_id with a new encrypted token."""
    with cursor(name='update_token', transaction=True) as c:
        c.execute('UPDATE tokens SET token = ? WHERE client_id = ?',
                  (token, client_id))


@app.teardown_appcontext
def close(exception):
    """Ensure that connection gets closed when app teardown happens."""
    if getattr(g, '_oauth_database', None) is None:
        return
    connection, g._oauth_database = g._oauth_database, None
    with stats.DBLatencyHistorgram.labels(query='close').time():
        connection.close()
