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
    with app.open_resource('../schema.sql', mode='r') as f:
        schema = f.read()
    with get() as c:
        c.executescript(schema)


def get():
    """Get singleton SQLite database connection."""
    if getattr(g, '_oauth_database', None) is None:
        g._oauth_database = sqlite3.connect(
            app.config['OAUTH_DATABASE'],
            timeout=app.config['OAUTH_DATABASE_TIMEOUT'])
        if app.config['OAUTH_DATABASE_PRAGMA']:
            g._oauth_database.execute(app.config['OAUTH_DATABASE_PRAGMA'])
    return g._oauth_database


def vacuum():
    with get() as c:
        c.execute('VACUUM')


@contextlib.contextmanager
def cursor(name):
    """Get SQLite cursor with automatic commit if no exceptions are raised."""
    try:
        with stats.DBLatencyHistorgram.labels(query=name).time():
            with get() as connection:
                yield connection.cursor()
    except sqlite3.Error as e:
        # https://www.python.org/dev/peps/pep-0249/#exceptions for values.
        error = re.sub(r'(?!^)([A-Z])', r'_\1', e.__class__.__name__).lower()
        stats.DBErrorCounter.labels(query=name, error=error).inc()
        raise


@app.teardown_appcontext
def close(exception):
    """Ensure that connection gets closed when app teardown happens."""
    if getattr(g, '_oauth_database', None) is None:
        return
    connection, g._oauth_database = g._oauth_database, None
    connection.close()
