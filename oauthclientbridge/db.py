import contextlib
import sqlite3
import uuid

from flask import g

from oauthclientbridge import app, stats

IntegrityError = sqlite3.IntegrityError


def generate_id():
    return str(uuid.uuid4())


def initialize():
    with app.open_resource('schema.sql', mode='r') as f:
        schema = f.read()
    with cursor() as c:
        c.executescript(schema)


def get():
    """Get singleton SQLite database connection."""
    if getattr(g, '_oauth_connection', None) is None:
        g._oauth_connection = sqlite3.connect(app.config['OAUTH_DATABASE'])
    return g._oauth_connection


@contextlib.contextmanager
def cursor(name):
    """Get SQLite cursor with automatic commit if no exceptions are raised."""
    with stats.DBLatencyHistorgram.labels(query=name).time():
        with get() as connection:
            yield connection.cursor()


@app.teardown_appcontext
def close(exception):
    """Ensure that connection gets closed when app teardown happens."""
    c, g._oauth_connection = getattr(g, '_oauth_connection', None), None
    if c is not None:
        c.close()
