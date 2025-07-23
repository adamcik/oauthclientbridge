import click

from oauthclientbridge import create_app, db
from oauthclientbridge.settings import Settings

# TODO: Make sure this is installed with an entry point, or that it can be used
# via flask at least.

# TODO: This probably breaks the CLI...
settings = Settings()  # pyright: ignore[reportCallIssue]

app = create_app(settings)


@app.cli.command()
def initdb():  # type: ignore
    """Initializes the database."""
    click.echo("Initializing %s" % settings.database)
    db.initialize()


@app.cli.command()
def cleandb():  # type: ignore
    """Cleans database of stale data."""
    click.echo("Vacummed %s" % settings.database)
    db.vacuum()
