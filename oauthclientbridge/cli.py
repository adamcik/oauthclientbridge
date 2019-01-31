import click

from oauthclientbridge import app, db, rate_limit


@app.cli.command()
def initdb():
    """Initializes the database."""
    click.echo('Initializing %s' % app.config['OAUTH_DATABASE'])
    db.initialize()


@app.cli.command()
def cleandb():
    """Cleans database of stale data."""
    cleaned = rate_limit.clean()
    click.echo(' - Deleted %s stale buckets' % cleaned)

    click.echo(' - Vacummed')
    db.vacuum()
