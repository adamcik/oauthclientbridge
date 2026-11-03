import typer

from oauthclientbridge import db, settings

app = typer.Typer(help="Administer the bridge database.", add_completion=False)


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
