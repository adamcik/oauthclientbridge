import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from oauthclientbridge import db, types

CLIENT_ID = types.ClientId(uuid.UUID("00000000-0000-0000-0000-000000000001"))
ENCRYPTED_TOKEN = types.EncryptedToken(b"token")


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
    name: str
    query: str


@pytest.mark.parametrize(
    "case",
    [
        LookupCase(
            name="text token",
            query=(
                "INSERT INTO tokens (client_id, token) VALUES "
                "('00000000-0000-0000-0000-000000000001', 'token')"
            ),
        ),
        LookupCase(
            name="blob token",
            query=(
                "INSERT INTO tokens (client_id, token) VALUES "
                "('00000000-0000-0000-0000-000000000001', X'746F6B656E')"
            ),
        ),
    ],
    ids=lambda case: case.name,
)
def test_lookup(case: LookupCase, cursor):
    cursor.execute(case.query)
    record = db.lookup(CLIENT_ID)
    assert ENCRYPTED_TOKEN == record.encrypted_token


def test_lookup_missing(cursor):
    with pytest.raises(LookupError):
        db.lookup(CLIENT_ID)


def test_is_initialized_uses_check_tokens_table_operation_name(cursor):
    with patch.object(db, "cursor", wraps=db.cursor) as mocked_cursor:
        db.is_initialized()

    mocked_cursor.assert_called_once_with(name="check_tokens_table", connection=None)


def test_lookup_revoked(cursor):
    cursor.execute(
        "INSERT INTO tokens (client_id) VALUES ('00000000-0000-0000-0000-000000000001')"
    )
    record = db.lookup(CLIENT_ID)
    assert record.encrypted_token is None


def test_lookup_includes_timestamps(cursor):
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

    assert db.lookup(CLIENT_ID) == db.TokenRecord(
        client_id=CLIENT_ID,
        encrypted_token=ENCRYPTED_TOKEN,
        created_at=created_at,
        last_updated_at=last_updated_at,
    )


TOKEN_TYPE_QUERY = "SELECT token, typeof(token) FROM tokens WHERE client_id = ?"


def test_insert(cursor):
    client_id = types.ClientId(uuid.UUID("00000000-0000-0000-0000-000000000002"))
    db.insert(client_id, ENCRYPTED_TOKEN)

    cursor.execute(
        "SELECT token, typeof(token), created_at, last_updated_at FROM tokens WHERE client_id = ?",
        (str(client_id),),
    )
    result, dbtype, created_at, last_updated_at = cursor.fetchone()
    assert b"token" == result
    assert b"text" == dbtype
    assert created_at is not None
    assert last_updated_at is not None


def test_update(cursor):
    cursor.execute(
        "INSERT INTO tokens (client_id, last_updated_at) VALUES "
        "('00000000-0000-0000-0000-000000000001', 1)"
    )

    assert 1 == db.update(CLIENT_ID, ENCRYPTED_TOKEN)

    cursor.execute(
        "SELECT token, typeof(token), last_updated_at FROM tokens WHERE client_id = ?",
        (str(CLIENT_ID),),
    )
    result, dbtype, last_updated_at = cursor.fetchone()
    assert b"token" == result
    assert b"text" == dbtype
    assert last_updated_at != 1


def test_update_none(cursor):
    cursor.execute(
        "INSERT INTO tokens (client_id, token, last_updated_at) VALUES "
        "('00000000-0000-0000-0000-000000000001', 'token', 1)"
    )

    assert 1 == db.update(CLIENT_ID, None)

    cursor.execute(
        "SELECT token, typeof(token), last_updated_at FROM tokens WHERE client_id = ?",
        (str(CLIENT_ID),),
    )
    result, dbtype, last_updated_at = cursor.fetchone()
    assert result is None
    assert b"null" == dbtype
    assert last_updated_at != 1


def test_update_missing(app_context):
    assert 0 == db.update(CLIENT_ID, ENCRYPTED_TOKEN)


def test_upgrade_adds_timestamp_columns_without_dropping_rows(app_context, cursor):
    cursor.execute("DROP TABLE tokens")
    cursor.execute("CREATE TABLE tokens(client_id text primary key, token blob)")
    cursor.execute(
        "INSERT INTO tokens (client_id, token) VALUES "
        "('00000000-0000-0000-0000-000000000001', 'token')"
    )

    db.upgrade()

    assert db.lookup(CLIENT_ID) == db.TokenRecord(
        client_id=CLIENT_ID,
        encrypted_token=ENCRYPTED_TOKEN,
        created_at=None,
        last_updated_at=None,
    )
