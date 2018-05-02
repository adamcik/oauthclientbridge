import contextlib
import sqlite3
import re
import uuid

from flask import g

from oauthclientbridge import app, stats

IntegrityError = sqlite3.IntegrityError


def generate_id():
    return str(uuid.uuid4())


def initialize(db):
    if db == 'tokens':
        schema = '../schema.sql'
    elif db == 'rate_limits':
        schema = '../schema.sql'
    else:
        raise LookupError('%r is not a valid database type.' % db)

    with app.open_resource(schema, mode='r') as f:
        schema = f.read()
    with cursor(db, name='init') as c:
        c.executescript(schema)


def get(db):
    """Get singleton SQLite database connection."""
    if db == 'tokens':
        path = app.config['OAUTH_DATABASE']
    elif db == 'rate_limits':
        path = app.config['OAUTH_RATE_LIMIT_DATABASE']
    else:
        raise LookupError('%r is not a valid database type.' % db)

    if not path:
        # Fallback to using tokens database if rate_limits not set.
        path = app.config['OAUTH_DATABASE']

    if getattr(g, '_oauth_databases', None) is None:
        g._oauth_databases = {}
    if db not in g._oauth_databases:
        g._oauth_databases[db] = sqlite3.connect(path)
    return g._oauth_databases[db]


@contextlib.contextmanager
def cursor(db, name):
    """Get SQLite cursor with automatic commit if no exceptions are raised."""
    try:
        with stats.DBLatencyHistorgram.labels(db=db, query=name).time():
            with get(db) as connection:
                yield connection.cursor()
    except sqlite3.Error as e:
        # https://www.python.org/dev/peps/pep-0249/#exceptions for values.
        error = '_'.join(re.findall('([A-Z]+[a-z]+)', e.__class__.__name__))
        stats.DBErrorCounter.labels(db=db, query=name, error=error.lower()).inc()
        raise


@app.teardown_appcontext
def close(exception):
    """Ensure that connection gets closed when app teardown happens."""
    if getattr(g, '_oauth_databases', None) is None:
        return
    databases, g._oauth_databases = g._oauth_databases, None
    for connection in databases.values():
        connection.close()
