import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from oauthclientbridge import db, types
from oauthclientbridge.settings import DatabaseSettings

CLIENT_ID = types.ClientId(uuid.UUID("00000000-0000-0000-0000-000000000001"))
ENCRYPTED_TOKEN = types.EncryptedToken(b"token")


@pytest.fixture
def database(tmp_path: Path) -> DatabaseSettings:
    settings = DatabaseSettings(database=str(tmp_path / "tokens.sqlite"))
    db.initialize(settings)
    return settings


@pytest.fixture
def cursor(database: DatabaseSettings):
    with sqlite3.connect(database.database, isolation_level=None) as connection:
        connection.text_factory = bytes
        yield connection.cursor()


@dataclass(frozen=True)
class DatabaseConnectCase:
    id: str
    database: str
    expected: tuple[str, bool]


@pytest.mark.parametrize(
    "case",
    [
        DatabaseConnectCase(
            id="memory-database",
            database=":memory:",
            expected=("file:oauthclientbridge?mode=memory&cache=shared", True),
        ),
        DatabaseConnectCase(
            id="memory-uri",
            database="file:isolated?mode=memory&cache=shared",
            expected=("file:isolated?mode=memory&cache=shared", True),
        ),
        DatabaseConnectCase(
            id="file-path",
            database="./sqlite.db",
            expected=("./sqlite.db", False),
        ),
    ],
    ids=lambda case: case.id,
)
def test_database_connect_args_supports_sqlite_uris(
    case: DatabaseConnectCase,
) -> None:
    assert (
        db._database_connect_args(DatabaseSettings(database=case.database))
        == case.expected
    )  # pyright: ignore[reportPrivateUsage] # Direct connection configuration test.


def test_connect_closes_connection_after_operation(database: DatabaseSettings) -> None:
    with db._connect(database) as connection:  # pyright: ignore[reportPrivateUsage] # Direct connection lifetime test.
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize(
    "value",
    [
        "00000000-0000-0000-0000-000000000001",
        "00000000000000000000000000000001",
    ],
)
def test_validate_client_id(value: str) -> None:
    assert db.validate_client_id(value) == uuid.UUID(
        "00000000-0000-0000-0000-000000000001"
    )


def test_validate_client_id_rejects_malformed_value() -> None:
    with pytest.raises(ValueError, match="badly formed"):
        _ = db.validate_client_id("not-a-uuid")


@dataclass(frozen=True)
class LookupCase:
    id: str
    query: str


@pytest.mark.parametrize(
    "case",
    [
        LookupCase(
            id="text-token",
            query=(
                "INSERT INTO tokens (client_id, token) VALUES "
                "('00000000-0000-0000-0000-000000000001', 'token')"
            ),
        ),
        LookupCase(
            id="blob-token",
            query=(
                "INSERT INTO tokens (client_id, token) VALUES "
                "('00000000-0000-0000-0000-000000000001', X'746F6B656E')"
            ),
        ),
    ],
    ids=lambda case: case.id,
)
def test_lookup(case: LookupCase, cursor: sqlite3.Cursor, database: DatabaseSettings):
    cursor.execute(case.query)
    record = db.lookup(CLIENT_ID, database)
    assert ENCRYPTED_TOKEN == record.encrypted_token


def test_lookup_missing(database: DatabaseSettings):
    with pytest.raises(LookupError):
        db.lookup(CLIENT_ID, database)


def test_is_initialized_uses_check_tokens_table_operation_name(
    database: DatabaseSettings,
):
    with patch.object(db, "cursor", wraps=db.cursor) as mocked_cursor:
        assert db.is_initialized(database)

    assert mocked_cursor.call_count == 1
    assert mocked_cursor.call_args.kwargs["name"] == "check_tokens_table"


def test_token_state_counts_uses_explicit_database_outside_flask(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "sqlite.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE tokens(client_id TEXT PRIMARY KEY, token BLOB);
            INSERT INTO tokens VALUES ('present', 'token');
            INSERT INTO tokens VALUES ('revoked', NULL);
            """
        )

    assert db.token_state_counts(DatabaseSettings(database=str(database_path))) == {
        "present": 1,
        "revoked": 1,
    }


def test_lookup_revoked(cursor: sqlite3.Cursor, database: DatabaseSettings):
    cursor.execute(
        "INSERT INTO tokens (client_id) VALUES ('00000000-0000-0000-0000-000000000001')"
    )
    record = db.lookup(CLIENT_ID, database)
    assert record.encrypted_token is None


def test_lookup_includes_timestamps(cursor: sqlite3.Cursor, database: DatabaseSettings):
    created_at = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    last_updated_at = datetime(2026, 6, 18, 13, 0, tzinfo=UTC)
    cursor.execute(
        (
            "INSERT INTO tokens (client_id, token, created_at, last_updated_at) "
            "VALUES (?, ?, ?, ?)"
        ),
        (
            str(CLIENT_ID),
            "token",
            int(created_at.timestamp()),
            int(last_updated_at.timestamp()),
        ),
    )

    assert db.lookup(CLIENT_ID, database) == db.TokenRecord(
        client_id=CLIENT_ID,
        encrypted_token=ENCRYPTED_TOKEN,
        created_at=created_at,
        last_updated_at=last_updated_at,
    )


TOKEN_TYPE_QUERY = "SELECT token, typeof(token) FROM tokens WHERE client_id = ?"


def test_insert(cursor: sqlite3.Cursor, database: DatabaseSettings):
    client_id = types.ClientId(uuid.UUID("00000000-0000-0000-0000-000000000002"))
    db.insert(client_id, ENCRYPTED_TOKEN, database)

    cursor.execute(
        "SELECT token, typeof(token), created_at, last_updated_at FROM tokens WHERE client_id = ?",
        (str(client_id),),
    )
    result, dbtype, created_at, last_updated_at = cursor.fetchone()
    assert b"token" == result
    assert b"text" == dbtype
    assert created_at is not None
    assert last_updated_at is not None


def test_update(cursor: sqlite3.Cursor, database: DatabaseSettings):
    cursor.execute(
        "INSERT INTO tokens (client_id, last_updated_at) VALUES "
        "('00000000-0000-0000-0000-000000000001', 1)"
    )

    assert 1 == db.update(CLIENT_ID, ENCRYPTED_TOKEN, database)

    cursor.execute(
        "SELECT token, typeof(token), last_updated_at FROM tokens WHERE client_id = ?",
        (str(CLIENT_ID),),
    )
    result, dbtype, last_updated_at = cursor.fetchone()
    assert b"token" == result
    assert b"text" == dbtype
    assert last_updated_at != 1


def test_update_none(cursor: sqlite3.Cursor, database: DatabaseSettings):
    cursor.execute(
        "INSERT INTO tokens (client_id, token, last_updated_at) VALUES "
        "('00000000-0000-0000-0000-000000000001', 'token', 1)"
    )

    assert 1 == db.update(CLIENT_ID, None, database)

    cursor.execute(
        "SELECT token, typeof(token), last_updated_at FROM tokens WHERE client_id = ?",
        (str(CLIENT_ID),),
    )
    result, dbtype, last_updated_at = cursor.fetchone()
    assert result is None
    assert b"null" == dbtype
    assert last_updated_at != 1


def test_update_missing(database: DatabaseSettings):
    assert 0 == db.update(CLIENT_ID, ENCRYPTED_TOKEN, database)


def test_upgrade_adds_timestamp_columns_without_dropping_rows(
    database: DatabaseSettings, cursor: sqlite3.Cursor
):
    cursor.execute("DROP TABLE tokens")
    cursor.execute("CREATE TABLE tokens(client_id text primary key, token blob)")
    cursor.execute(
        "INSERT INTO tokens (client_id, token) VALUES "
        "('00000000-0000-0000-0000-000000000001', 'token')"
    )

    db.upgrade(database)

    assert db.lookup(CLIENT_ID, database) == db.TokenRecord(
        client_id=CLIENT_ID,
        encrypted_token=ENCRYPTED_TOKEN,
        created_at=None,
        last_updated_at=None,
    )
