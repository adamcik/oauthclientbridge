import json

import pytest

try:
    from urllib import parse as urlparse
except ImportError:
    import urlparse

from oauthclientbridge import app, crypto, db
from oauthclientbridge.errors import *


def test_authorize_redirects(client):
    resp = client.get('/')
    location = urlparse.urlparse(resp.location)
    params = urlparse.parse_qs(location.query)

    assert resp.status_code == 302
    assert location.netloc == 'provider.example.com'
    assert location.path == '/auth'

    with client.session_transaction() as session:
        assert 'state' in session


def test_authorize_wrong_method(client):
    resp = client.post('/')
    assert resp.status_code == 405


@pytest.mark.parametrize('query,expected_error', [
    ('', INVALID_STATE),
    ('?code', INVALID_STATE),
    ('?code=1234', INVALID_STATE),
    ('?state={state}', INVALID_REQUEST),
    ('?state={state}&code', INVALID_REQUEST),
    ('?state={state}&error=invalid_request', INVALID_REQUEST),
    ('?state={state}&error=unauthorized_client', UNAUTHORIZED_CLIENT),
    ('?state={state}&error=access_denied', ACCESS_DENIED),
    ('?state={state}&error=unsupported_response_type',
     UNSUPPORTED_RESPONSE_TYPE),
    ('?state={state}&error=invalid_scope', INVALID_SCOPE),
    ('?state={state}&error=server_error', SERVER_ERROR),
    ('?state={state}&error=temporarily_unavailable', TEMPORARILY_UNAVAILABLE),
    ('?state={state}&error=badErrorCode', SERVER_ERROR),
])
def test_callback_error_handling(query, expected_error, client, state):
    app.config['OAUTH_CALLBACK_TEMPLATE'] = '{{error}}'

    resp = client.get('/callback' + query.format(state=state))
    assert resp.status_code == 400
    assert resp.data == expected_error


# TODO: Revisit all of the status codes returned, since this is not an API
# endpoint but a callback we can be well behaved with respect to HTTP.
@pytest.mark.parametrize('data,expected_error,expected_status', [
    ({}, INVALID_RESPONSE, 400),
    ({'token_type': 'foobar'}, INVALID_RESPONSE, 400),
    ({'access_token': 'foobar'}, INVALID_RESPONSE, 400),
    ({'access_token': '', 'token_type': ''}, INVALID_RESPONSE, 400),
    ({'access_token': 'foobar', 'token_type': ''}, INVALID_RESPONSE, 400),
    ({'access_token': '', 'token_type': 'foobar'}, INVALID_RESPONSE, 400),
    ({'error': INVALID_REQUEST}, INVALID_REQUEST, 400),
    ({'error': INVALID_CLIENT}, INVALID_CLIENT, 401),
    ({'error': INVALID_GRANT}, INVALID_GRANT, 400),
    ({'error': UNAUTHORIZED_CLIENT}, UNAUTHORIZED_CLIENT, 400),
    ({'error': UNSUPPORTED_GRANT_TYPE}, UNSUPPORTED_GRANT_TYPE, 400),
    ({'error': INVALID_SCOPE}, INVALID_SCOPE, 400),
    ({'error': SERVER_ERROR}, SERVER_ERROR, 400),
    ({'error': TEMPORARILY_UNAVAILABLE}, TEMPORARILY_UNAVAILABLE, 400),
    ({'error': 'errorTransient'}, TEMPORARILY_UNAVAILABLE, 400),
    ({'error': 'badErrorCode'}, SERVER_ERROR, 400),
])
def test_callback_authorization_code_error_handling(
        data, expected_error, expected_status, client, state, requests_mock):
    app.config['OAUTH_CALLBACK_TEMPLATE'] = '{{error}}'

    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=data)

    resp = client.get('/callback?code=1234&state=' + state)
    assert resp.status_code == expected_status
    assert resp.data == expected_error


# TODO: Test with more status codes from callback...
def test_callback_authorization_code_invalid_response(
        client, state, requests_mock):
    app.config['OAUTH_CALLBACK_TEMPLATE'] = '{{error}}'

    requests_mock.post(app.config['OAUTH_TOKEN_URI'], text='Not a JSON value')

    resp = client.get('/callback?code=1234&state=' + state)
    assert resp.status_code == 400
    assert resp.data == SERVER_ERROR


def test_callback_authorization_code_stores_token(client, state, requests_mock):
    app.config['OAUTH_CALLBACK_TEMPLATE'] = '{{client_id}}:{{client_secret}}'

    data = {'token_type': 'Bearer', 'access_token': '1234567890'}
    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=data)

    resp = client.get('/callback?code=1234&state=' + state)
    client_id, client_secret = resp.data.split(':')

    # Peek inside internals to check that our token got stored.
    assert data == crypto.loads(client_secret, db.lookup(client_id))


def test_callback_authorization_code_store_refresh_token(
        client, state, requests_mock):
    app.config['OAUTH_CALLBACK_TEMPLATE'] = '{{client_id}}:{{client_secret}}'

    token = {'token_type': 'test', 'refresh_token': 'abc','scope': 'foo',
             'access_token': '123', 'expires_in': 3600}
    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=token)

    resp = client.get('/callback?code=1234&state=' + state)
    client_id, client_secret = resp.data.split(':')

    # Peek inside internals to check that our token got stored.
    expected = {'refresh_token': 'abc', 'scope': 'foo'}
    assert expected == crypto.loads(client_secret, db.lookup(client_id))


def test_callback_authorization_code_store_unknown(
        client, state, requests_mock):
    app.config['OAUTH_CALLBACK_TEMPLATE'] = '{{client_id}}:{{client_secret}}'

    data = {'token_type': 'Bearer', 'access_token': '123', 'private': 'foobar'}
    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=data)

    resp = client.get('/callback?code=1234&state=' + state)
    client_id, client_secret = resp.data.split(':')

    # Peek inside internals to check that our token got stored.
    assert data == crypto.loads(client_secret, db.lookup(client_id))


def test_callack_wrong_method(client, state):
    resp = client.post('/callback?code=1234&state=' + state)
    assert resp.status_code == 405


# TODO: Duplicate client-id handling?
# TODO: Wrong methods?
