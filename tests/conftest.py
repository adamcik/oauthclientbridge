import base64
import collections
import json

import pytest

from oauthclientbridge import app, crypto, db

TestToken = collections.namedtuple(
    'TestToken', ('client_id', 'client_secret', 'value'))


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
def post(client):
    def _post(path, data, auth=None):
        if auth:
            encoded = base64.b64encode('%s:%s' % auth)
            headers = {'Authorization': 'Basic %s' % encoded}
        else:
            headers = {}

        resp = client.post(path, headers=headers, data=data)
        return json.loads(resp.data), resp.status_code

    return _post


@pytest.fixture
def state(client):
    with client.session_transaction() as session:
        session['state'] = 'abcdef'
    return 'abcdef'


def _test_token(**data):
    client_secret = crypto.generate_key()
    token = crypto.dumps(client_secret, data)
    client_id = db.insert(token)
    return TestToken(client_id, client_secret, data)


@pytest.fixture
def access_token():
    return _test_token(token_type='test', access_token='123', expires_in=3600)


@pytest.fixture
def refresh_token():
    return _test_token(token_type='test', refresh_token='abc')
