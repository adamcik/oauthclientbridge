import contextlib
import re
import sqlite3
import uuid
from typing import Iterator, Optional

from flask import g

from oauthclientbridge import app, stats

Error = sqlite3.Error
IntegrityError = sqlite3.IntegrityError


def generate_id() -> str:
    return str(uuid.uuid4())


def initialize() -> None:
    with app.open_resource("schema.sql", mode="r") as f:
        schema = f.read()
    with get() as c:
        c.executescript(schema)


def get() -> sqlite3.Connection:
    """Get singleton SQLite database connection."""
    if getattr(g, "_oauth_database", None) is None:
        connection = sqlite3.connect(
            app.config["OAUTH_DATABASE"],
            timeout=app.config["OAUTH_DATABASE_TIMEOUT"],
            isolation_level=None,
        )
        g._oauth_database = connection
        g._oauth_database.text_factory = lambda v: v
        for pragma in app.config["OAUTH_DATABASE_PRAGMAS"]:
            g._oauth_database.execute(pragma)
    return g._oauth_database


def vacuum() -> None:
    with get() as c:
        c.execute("VACUUM")


@contextlib.contextmanager
def cursor(name: str, transaction: bool = False) -> Iterator[sqlite3.Cursor]:
    """Get SQLite cursor with automatic commit if no exceptions are raised."""
    try:
        with get() as connection:
            c = connection.cursor()
            with contextlib.closing(c):
                with stats.DBLatencyHistorgram.labels(query=name).time():
                    try:
                        if transaction:
                            c.execute("BEGIN")
                        yield c
                    except Exception:
                        if transaction:
                            connection.rollback()
                        raise
                    else:
                        if transaction:
                            connection.commit()
    except sqlite3.Error as e:
        # https://www.python.org/dev/peps/pep-0249/#exceptions for values.
        error = re.sub(r"(?!^)([A-Z])", r"_\1", e.__class__.__name__).lower()
        stats.DBErrorCounter.labels(query=name, error=error).inc()
        raise


def _prepare_token(token: Optional[bytes]) -> Optional[str]:
    """Convert token to str so it gets stored as text type in sqlite3.

    This is primarily to make it nicer to inspect the DB when debugging as the
    token is base64 encoded, not raw bytes.
    """
    return None if token is None else token.decode("ascii")


def insert(token: bytes) -> str:
    """Store encrypted token and return what client_id it was stored under."""
    client_id = generate_id()

    with cursor(name="insert_token", transaction=True) as c:
        # TODO: Retry creating client_id if it already exists?
        c.execute(
            "INSERT INTO tokens (client_id, token) VALUES (?, ?)",
            (client_id, _prepare_token(token)),
        )
    return client_id


def lookup(client_id: str) -> Optional[bytes]:
    """Lookup a client_id and return encrypted token.

    Raises a LookupError if client_id is not found.
    Returns the encrypted token or None if token is revoked.
    """
    with cursor(name="lookup_token") as c:
        c.execute("SELECT token FROM tokens WHERE client_id = ?", (client_id,))
        row = c.fetchone()

    if row is None:
        raise LookupError("Client not found.")
    elif row[0]:
        # Fernet only likes bytes, so return token as such. DB might contain
        # tokens stored as TEXT, BLOB or NULL types.
        return bytes(row[0])
    else:
        return None


def update(client_id: str, token: Optional[bytes]) -> int:
    """Update a client_id with a new encrypted token."""

    with cursor(name="update_token", transaction=True) as c:
        c.execute(
            "UPDATE tokens SET token = ? WHERE client_id = ?",
            (_prepare_token(token), client_id),
        )
        return int(c.rowcount)


@app.teardown_appcontext
def close(exception):
    """Ensure that connection gets closed when app teardown happens."""
    if getattr(g, "_oauth_database", None) is None:
        return
    connection, g._oauth_database = g._oauth_database, None
    connection.close()
