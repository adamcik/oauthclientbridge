import pytest

from oauthclientbridge import app, db


@pytest.fixture
def client():
    app.config.update({
        'TESTING': True,
        'SECRET_KEY': 's3cret',
        'OAUTH_DATABASE': ':memory:',
        'OAUTH_CLIENT_ID': 'client',
        'OAUTH_CLIENT_SECRET': 's3cret',
        'OAUTH_AUTHORIZATION_URI': 'https://provider.example.com/auth',
        'OAUTH_TOKEN_URI': 'https://provider.example.com/token',
        'OAUTH_REDIRECT_URI': 'https://client.example.com/callback',
    })

    client = app.test_client()

    with app.app_context():
        db.initialize()
        yield client


@pytest.fixture
def state(client):
    with client.session_transaction() as session:
        session['state'] = 'abcdef'
    return 'abcdef'
