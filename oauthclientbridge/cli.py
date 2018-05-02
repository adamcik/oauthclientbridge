import click

from oauthclientbridge import app, db, rate_limit


@app.cli.command()
@click.option('--name', type=click.Choice(['tokens', 'rate_limits']))
def initdb(name):
    """Initializes the database."""
    click.echo('Initializing %s' % name)
    db.initialize(name)


@app.cli.command()
def cleandb():
    """Cleans database of stale data."""
    cleaned = rate_limit.clean()
    click.echo('Deleted %s stale buckets' % cleaned)
