import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from oauthclientbridge import cli


def test_initdb_creates_schema(tmp_path: Path) -> None:
    database = tmp_path / "sqlite.db"
    result = CliRunner().invoke(cli.app, ["initdb"], env={"DB_DATABASE": str(database)})

    assert result.exit_code == 0, result.output
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tokens)")}
    assert columns == {"client_id", "token", "created_at", "last_updated_at"}


def test_upgradedb_preserves_tokens_and_can_run_again(tmp_path: Path) -> None:
    database = tmp_path / "sqlite.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE tokens(client_id TEXT PRIMARY KEY, token BLOB);"
            "INSERT INTO tokens VALUES ('client', X'746F6B656E');"
        )

    for _ in range(2):
        result = CliRunner().invoke(
            cli.app, ["upgradedb"], env={"DB_DATABASE": str(database)}
        )
        assert result.exit_code == 0, result.output

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM tokens").fetchall() == [
            ("client", b"token", None, None)
        ]


def test_cleandb_reclaims_space_without_removing_tokens(tmp_path: Path) -> None:
    database = tmp_path / "sqlite.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE tokens(client_id TEXT PRIMARY KEY, token BLOB);"
            "INSERT INTO tokens VALUES ('keep', X'746F6B656E');"
            "INSERT INTO tokens VALUES ('remove', zeroblob(100000));"
            "DELETE FROM tokens WHERE client_id = 'remove';"
        )
        assert connection.execute("PRAGMA freelist_count").fetchone()[0] > 0

    result = CliRunner().invoke(
        cli.app, ["cleandb"], env={"DB_DATABASE": str(database)}
    )

    assert result.exit_code == 0, result.output
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA freelist_count").fetchone()[0] == 0
        assert connection.execute("SELECT * FROM tokens").fetchall() == [
            ("keep", b"token")
        ]


def test_database_failure_exits_nonzero(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli.app,
        ["initdb"],
        env={"DB_DATABASE": str(tmp_path / "missing" / "sqlite.db")},
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, sqlite3.OperationalError)
