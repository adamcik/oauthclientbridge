import contextlib
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterator

from flask import current_app, g
from opentelemetry import metrics, trace

from oauthclientbridge import stats
from oauthclientbridge.settings import current_settings
from oauthclientbridge.utils import utcnow

Error = sqlite3.Error
IntegrityError = sqlite3.IntegrityError

tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

_db_cursor_total_counter = meter.create_counter(
    name="oauth.db.cursor.total",
    description="Measures the total number of logical database operations.",
)

_db_cursor_duration_histogram = meter.create_histogram(
    name="oauth.db.cursor.duration",
    description="Measures the duration of a logical database operation.",
    unit="s",
)


def generate_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class TokenRecord:
    client_id: str
    encrypted_token: bytes | None
    created_at: datetime | None
    last_updated_at: datetime | None


def initialize() -> None:
    with current_app.open_resource("schema.sql", mode="r") as f:
        schema = f.read()
    with get() as c:
        c.executescript(schema)


def upgrade() -> None:
    with get() as c:
        columns = {
            row[1].decode("ascii") if isinstance(row[1], bytes) else row[1]
            for row in c.execute("PRAGMA table_info(tokens)").fetchall()
        }
        if "created_at" not in columns:
            c.execute("ALTER TABLE tokens ADD COLUMN created_at INTEGER")
        if "last_updated_at" not in columns:
            c.execute("ALTER TABLE tokens ADD COLUMN last_updated_at INTEGER")


# TODO: Make this internal in favour of always needing to have a cursor
# https://github.com/open-telemetry/opentelemetry-python-contrib/issues/3082
# is the driver for this idea, as connection.execute() is not instrumented.
def get() -> sqlite3.Connection:
    """Get singleton SQLite database connection."""
    if getattr(g, "_oauth_database", None) is None:
        connection = sqlite3.connect(
            current_settings.database.database,
            timeout=current_settings.database.timeout,
            isolation_level=None,
        )
        g._oauth_database = connection
        g._oauth_database.text_factory = lambda v: v
        for pragma in current_settings.database.pragmas:
            g._oauth_database.execute(pragma)

    return g._oauth_database


def vacuum() -> None:
    with get() as c:
        c.execute("VACUUM")


@contextlib.contextmanager
def cursor(name: str, transaction: bool = False) -> Iterator[sqlite3.Cursor]:
    """Get SQLite cursor with automatic commit if no exceptions are raised."""
    start_time = time.monotonic()
    attributes = {
        "db.operation": name,
        "transaction": transaction,
        "db.system": "sqlite",
        "db.name": current_settings.database.database,
    }
    with tracer.start_as_current_span(
        f"DB {name}", attributes={"transaction": transaction}
    ) as span:
        try:
            with get() as connection:
                c = connection.cursor()
                with contextlib.closing(c):
                    with stats.DBLatencyHistorgram.labels(query=name).time():
                        try:
                            if transaction:
                                c.execute("BEGIN")
                            yield c
                        except Exception as e:
                            span.record_exception(e)
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

            attributes["error.type"] = e.__class__.__name__
            raise
        finally:
            duration = time.monotonic() - start_time
            _db_cursor_duration_histogram.record(duration, attributes=attributes)
            _db_cursor_total_counter.add(1, attributes=attributes)


def _prepare_token(token: bytes | None) -> str | None:
    """Convert token to str so it gets stored as text type in sqlite3.

    This is primarily to make it nicer to inspect the DB when debugging as the
    token is base64 encoded, not raw bytes.
    """
    return None if token is None else token.decode("ascii")


def _prepare_timestamp(value: datetime | None) -> int | None:
    return None if value is None else int(value.astimezone(UTC).timestamp())


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = int(value.decode("ascii"))
    if not isinstance(value, int):
        raise TypeError(f"Unsupported datetime value: {type(value)!r}")
    return datetime.fromtimestamp(value, UTC)


def insert(client_id: str, token: bytes) -> None:
    """Store encrypted token and return what client_id it was stored under."""

    now = utcnow()
    with cursor(name="insert_token", transaction=True) as c:
        c.execute(
            (
                "INSERT INTO tokens "
                "(client_id, token, created_at, last_updated_at) VALUES (?, ?, ?, ?)"
            ),
            (
                client_id,
                _prepare_token(token),
                _prepare_timestamp(now),
                _prepare_timestamp(now),
            ),
        )

    stats.set_token_state_counts(token_state_counts())


def lookup(client_id: str) -> TokenRecord:
    """Lookup a client_id and return encrypted token plus metadata.

    Raises a LookupError if client_id is not found.
    Returns the encrypted token or None if token is revoked.
    """
    with cursor(name="lookup_token") as c:
        c.execute(
            "SELECT token, created_at, last_updated_at FROM tokens WHERE client_id = ?",
            (client_id,),
        )
        row = c.fetchone()

    if row is None:
        raise LookupError("Client not found.")

    token_value = row[0]
    return TokenRecord(
        client_id=client_id,
        encrypted_token=bytes(token_value) if token_value else None,
        created_at=_parse_datetime(row[1]),
        last_updated_at=_parse_datetime(row[2]),
    )


def update(client_id: str, token: bytes | None) -> int:
    """Update a client_id with a new encrypted token."""

    now = utcnow()
    with cursor(name="update_token", transaction=True) as c:
        c.execute(
            "UPDATE tokens SET token = ?, last_updated_at = ? WHERE client_id = ?",
            (_prepare_token(token), _prepare_timestamp(now), client_id),
        )
        trace.get_current_span().add_event("Update result", {"rows": c.rowcount})
        rowcount = int(c.rowcount)

    if rowcount:
        stats.set_token_state_counts(token_state_counts())

    return rowcount


def token_state_counts() -> dict[str, int]:
    """Count stored token records by coarse database state."""

    with cursor(name="count_token_states") as c:
        c.execute(
            """
            SELECT
                SUM(CASE WHEN token IS NOT NULL THEN 1 ELSE 0 END) AS present,
                SUM(CASE WHEN token IS NULL THEN 1 ELSE 0 END) AS revoked
            FROM tokens
            """
        )
        row = c.fetchone()

    present = int(row[0] or 0) if row is not None else 0
    revoked = int(row[1] or 0) if row is not None else 0
    return {"present": present, "revoked": revoked}


def close(exception: BaseException | None) -> None:
    """Ensure that connection gets closed when app teardown happens."""
    if getattr(g, "_oauth_database", None) is None:
        return
    connection, g._oauth_database = g._oauth_database, None
    connection.close()
