import contextlib
import sqlite3
import re
import uuid

from flask import g

from oauthclientbridge import app, stats

IntegrityError = sqlite3.IntegrityError


def generate_id():
    return str(uuid.uuid4())


def initialize(database):
    # TODO: split into one schema file per table.
    if database in ('tokens', 'rate_limits'):
        schema = '../schema.sql'
    else:
        raise LookupError('%r is not a valid database type.' % database)

    with app.open_resource(schema, mode='r') as f:
        schema = f.read()
    with get(database) as c:
        c.executescript(schema)


def get(database):
    """Get singleton SQLite database connection."""
    if database == 'rate_limits' and not app.config['OAUTH_RATE_LIMIT_DATABASE']:
        database = 'tokens'

    if database == 'tokens':
        path = app.config['OAUTH_DATABASE']
    elif database == 'rate_limits':
        path = app.config['OAUTH_RATE_LIMIT_DATABASE']
    else:
        raise LookupError('%r is not a valid database type.' % database)

    if getattr(g, '_oauth_databases', None) is None:
        g._oauth_databases = {}

    if database not in g._oauth_databases:
        connection = sqlite3.connect(path)
        connection.execute('PRAGMA journal_mode = TRUNCATE')
        # TODO: Try this is we are using rate_limits DB?
        # connection.execute('PRAGMA synchronous = OFF')
        g._oauth_databases[database] = connection

    return g._oauth_databases[database]


def vacuum(database):
    with get(database) as c:
        c.execute('VACUUM')


@contextlib.contextmanager
def cursor(database, name):
    """Get SQLite cursor with automatic commit if no exceptions are raised."""
    try:
        with stats.DBLatencyHistorgram.labels(db=database, query=name).time():
            with get(database) as connection:
                yield connection.cursor()
    except sqlite3.Error as e:
        # https://www.python.org/dev/peps/pep-0249/#exceptions for values.
        error = re.sub(r'(?!^)([A-Z])', r'_\1', e.__class__.__name__).lower()
        stats.DBErrorCounter.labels(db=database, query=name, error=error).inc()
        raise


@app.teardown_appcontext
def close(exception):
    """Ensure that connection gets closed when app teardown happens."""
    if getattr(g, '_oauth_databases', None) is None:
        return
    databases, g._oauth_databases = g._oauth_databases, None
    for connection in databases.values():
        connection.close()
