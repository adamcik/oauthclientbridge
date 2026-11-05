import sys
from typing import Annotated

import typer
import uvicorn
from typer import (
    Option,  # pyright: ignore[reportUnknownVariableType] # Typer's Click ParamType annotation lacks a type argument.
)

from oauthclientbridge import db, settings

app = typer.Typer(help="Run and administer OAuth Client Bridge.", add_completion=False)


def main() -> None:
    app(args=sys.argv[1:] or ["serve"])


@app.command()
def serve(
    host: Annotated[str, Option()] = "127.0.0.1",
    port: Annotated[int, Option(min=1, max=65535)] = 8000,
    uds: Annotated[str | None, Option(help="Unix socket path.")] = None,
    fd: Annotated[
        int | None, Option(min=0, help="Inherited listening socket FD.")
    ] = None,
    workers: Annotated[int, Option(min=1, envvar="WORKERS")] = 4,
    timeout_graceful_shutdown: Annotated[
        int, Option(min=0, envvar="ASGI_GRACEFUL_SHUTDOWN_TIMEOUT")
    ] = 25,
) -> None:
    """Serve the ASGI application (the default when no command is given)."""
    uvicorn.run(
        "oauthclientbridge.asgi:create_app",
        factory=True,
        proxy_headers=False,
        host=host,
        port=port,
        uds=uds,
        fd=fd,
        workers=workers,
        timeout_graceful_shutdown=timeout_graceful_shutdown,
    )


@app.command()
def initdb() -> None:
    """Initialize the database configured by DB_DATABASE."""
    database = settings.DatabaseSettings()
    db.initialize(database)
    typer.echo(f"Initialized {database.database}")


@app.command()
def upgradedb() -> None:
    """Upgrade the database schema without removing existing tokens."""
    database = settings.DatabaseSettings()
    db.upgrade(database)
    typer.echo(f"Upgraded {database.database}")


@app.command()
def cleandb() -> None:
    """Vacuum the database to reclaim unused space."""
    database = settings.DatabaseSettings()
    db.vacuum(database)
    typer.echo(f"Vacuumed {database.database}")
