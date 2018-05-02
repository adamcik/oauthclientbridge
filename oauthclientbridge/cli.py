import click

from oauthclientbridge import app, db, rate_limit


@app.cli.command()
@click.argument('name', type=click.Choice(['tokens', 'rate_limits']))
def initdb(name):
    """Initializes the database."""
    click.echo('Initializing %s' % name)
    db.initialize(name)


@app.cli.command()
def cleandb():
    """Cleans database of stale data."""
    for name in ['tokens', 'rate_limits']:
        click.echo(name)

        cleaned = rate_limit.clean(name)
        click.echo(' - Deleted %s stale buckets' % cleaned)

        click.echo(' - Vacummed')
        db.vacuum(name)
