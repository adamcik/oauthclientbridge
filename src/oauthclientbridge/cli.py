import click

from oauthclientbridge import create_app, db

app = create_app()


@app.cli.command()
def initdb():  # type: ignore
    """Initializes the database."""
    click.echo("Initializing %s" % app.config["OAUTH_DATABASE"])
    db.initialize()


@app.cli.command()
def cleandb():  # type: ignore
    """Cleans database of stale data."""
    click.echo("Vacummed %s" % app.config["OAUTH_DATABASE"])
    db.vacuum()
