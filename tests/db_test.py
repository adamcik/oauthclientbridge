import pytest

from oauthclientbridge import db


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO tokens (client_id, token) VALUES ('client', 'token')",
        "INSERT INTO tokens (client_id, token) VALUES ('client', X'746F6B656E')",
    ],
)
def test_lookup(query, cursor):
    cursor.execute(query)
    assert b"token" == db.lookup("client")


def test_lookup_missing(cursor):
    with pytest.raises(LookupError):
        db.lookup("client")


def test_lookup_revoked(cursor):
    cursor.execute("INSERT INTO tokens (client_id) VALUES ('client')")
    assert db.lookup("client") is None


TOKEN_TYPE_QUERY = "SELECT token, typeof(token) FROM tokens WHERE client_id = ?"


def test_insert(cursor):
    client_id = db.insert(b"token")

    cursor.execute(TOKEN_TYPE_QUERY, (client_id,))
    result, dbtype = cursor.fetchone()
    assert b"token" == result
    assert b"text" == dbtype


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
