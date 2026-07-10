from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from oauthclientbridge import db


@dataclass(frozen=True)
class LookupCase:
    name: str
    query: str


@pytest.mark.parametrize(
    "case",
    [
        LookupCase(
            name="text token",
            query="INSERT INTO tokens (client_id, token) VALUES ('client', 'token')",
        ),
        LookupCase(
            name="blob token",
            query="INSERT INTO tokens (client_id, token) VALUES ('client', X'746F6B656E')",
        ),
    ],
    ids=lambda case: case.name,
)
def test_lookup(case: LookupCase, cursor):
    cursor.execute(case.query)
    record = db.lookup("client")
    assert b"token" == record.encrypted_token


def test_lookup_missing(cursor):
    with pytest.raises(LookupError):
        db.lookup("client")


def test_lookup_revoked(cursor):
    cursor.execute("INSERT INTO tokens (client_id) VALUES ('client')")
    record = db.lookup("client")
    assert record.encrypted_token is None


def test_lookup_includes_timestamps(cursor):
    created_at = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    cursor.execute(
        "INSERT INTO tokens (client_id, token, created_at) VALUES (?, ?, ?)",
        ("client", "token", int(created_at.timestamp())),
    )

    assert db.lookup("client") == db.TokenRecord(
        client_id="client",
        encrypted_token=b"token",
        created_at=created_at,
    )


TOKEN_TYPE_QUERY = "SELECT token, typeof(token) FROM tokens WHERE client_id = ?"


def test_insert(cursor):
    client_id = "test_client_id"
    db.insert(client_id, b"token")

    cursor.execute(
        "SELECT token, typeof(token), created_at FROM tokens WHERE client_id = ?",
        (client_id,),
    )
    result, dbtype, created_at = cursor.fetchone()
    assert b"token" == result
    assert b"text" == dbtype
    assert created_at is not None


def test_update(cursor):
    cursor.execute("INSERT INTO tokens (client_id) VALUES ('client')")

    assert 1 == db.update("client", b"token")

    cursor.execute(TOKEN_TYPE_QUERY, ("client",))
    result, dbtype = cursor.fetchone()
    assert b"token" == result
    assert b"text" == dbtype


def test_update_none(cursor):
    cursor.execute("INSERT INTO tokens (client_id, token) VALUES ('client', 'token')")

    assert 1 == db.update("client", None)

    cursor.execute(TOKEN_TYPE_QUERY, ("client",))
    result, dbtype = cursor.fetchone()
    assert result is None
    assert b"null" == dbtype


def test_update_missing(app_context):
    assert 0 == db.update("client", b"token")


def test_upgrade_adds_timestamp_columns_without_dropping_rows(app_context, cursor):
    cursor.execute("DROP TABLE tokens")
    cursor.execute("CREATE TABLE tokens(client_id text primary key, token blob)")
    cursor.execute("INSERT INTO tokens (client_id, token) VALUES ('client', 'token')")

    db.upgrade()

    assert db.lookup("client") == db.TokenRecord(
        client_id="client",
        encrypted_token=b"token",
        created_at=None,
    )
