import click

from oauthclientbridge import app, db


@app.cli.command()
def initdb():
    """Initializes the database."""
    click.echo('Initializing %s' % app.config['OAUTH_DATABASE'])
    db.initialize()


@app.cli.command()
def cleandb():
    """Cleans database of stale data."""
    click.echo('Vacummed %s' % app.config['OAUTH_DATABASE'])
    db.vacuum()
