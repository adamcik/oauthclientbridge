import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest
import uvicorn
from typer.testing import CliRunner

from oauthclientbridge import cli


@pytest.mark.parametrize("args", [[], ["serve"]], ids=["default", "explicit"])
def test_serve_starts_uvicorn(args: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["oauthclientbridge", *args])
    monkeypatch.delenv("WORKERS", raising=False)
    monkeypatch.delenv("ASGI_GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)

    # Check server configuration without starting worker processes or binding a port.
    with mock.patch.object(uvicorn, "run") as run:
        with pytest.raises(SystemExit) as exit:
            cli.main()

    assert exit.value.code == 0
    run.assert_called_once_with(
        "oauthclientbridge.asgi:create_app",
        factory=True,
        proxy_headers=False,
        host="127.0.0.1",
        port=8000,
        uds=None,
        workers=4,
        timeout_graceful_shutdown=25,
    )


def test_serve_uses_options_and_runtime_environment() -> None:
    with mock.patch.object(uvicorn, "run") as run:
        result = CliRunner().invoke(
            cli.app,
            ["serve", "--uds", "/run/asgi/server.sock", "--workers", "2"],
            env={"WORKERS": "3", "ASGI_GRACEFUL_SHUTDOWN_TIMEOUT": "10"},
        )

    assert result.exit_code == 0, result.output
    run.assert_called_once_with(
        "oauthclientbridge.asgi:create_app",
        factory=True,
        proxy_headers=False,
        host="127.0.0.1",
        port=8000,
        uds="/run/asgi/server.sock",
        workers=2,
        timeout_graceful_shutdown=10,
    )


def test_serve_rejects_invalid_worker_count() -> None:
    with mock.patch.object(uvicorn, "run") as run:
        result = CliRunner().invoke(cli.app, ["serve", "--workers", "0"])

    assert result.exit_code == 2
    run.assert_not_called()


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
