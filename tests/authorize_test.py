import json
import urlparse

import pytest

from oauthclientbridge import app, crypto, db


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
    ('', 'invalid_state'),
    ('?code', 'invalid_state'),
    ('?code=1234', 'invalid_state'),
    ('?state={state}', 'invalid_request'),
    ('?state={state}&code', 'invalid_request'),
    ('?state={state}&error=invalid_request', 'invalid_request'),
    ('?state={state}&error=unauthorized_client', 'unauthorized_client'),
    ('?state={state}&error=access_denied', 'access_denied'),
    ('?state={state}&error=unsupported_response_type',
     'unsupported_response_type'),
    ('?state={state}&error=invalid_scope', 'invalid_scope'),
    ('?state={state}&error=server_error', 'server_error'),
    ('?state={state}&error=temporarily_unavailable',
     'temporarily_unavailable'),
    ('?state={state}&error=badErrorCode', 'server_error'),
])
def test_callback_error_handling(query, expected_error, client, state):
    app.config['OAUTH_CALLBACK_TEMPLATE'] = '{{error}}'

    resp = client.get('/callback' + query.format(state=state))
    assert resp.status_code == 400
    assert resp.data == expected_error


# TODO: Revisit all of the status codes returned, since this is not an API
# endpoint but a callback we can be well behaved with respect to HTTP.
@pytest.mark.parametrize('data,expected_error,expected_status', [
    ({}, 'invalid_response', 400),
    ({'token_type': 'foobar'}, 'invalid_response', 400),
    ({'access_token': 'foobar'}, 'invalid_response', 400),
    ({'access_token': '', 'token_type': ''}, 'invalid_response', 400),
    ({'access_token': 'foobar', 'token_type': ''}, 'invalid_response', 400),
    ({'access_token': '', 'token_type': 'foobar'}, 'invalid_response', 400),
    ({'error': 'invalid_request'}, 'invalid_request', 400),
    ({'error': 'invalid_client'}, 'invalid_client', 401),
    ({'error': 'invalid_grant'}, 'invalid_grant', 400),
    ({'error': 'unauthorized_client'}, 'unauthorized_client', 400),
    ({'error': 'unsupported_grant_type'}, 'unsupported_grant_type', 400),
    ({'error': 'invalid_scope'}, 'invalid_scope', 400),
    ({'error': 'server_error'}, 'server_error', 400),
    ({'error': 'temporarily_unavailable'}, 'temporarily_unavailable', 400),
    ({'error': 'errorTransient'}, 'temporarily_unavailable', 400),
    ({'error': 'badErrorCode'}, 'server_error', 400),
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
    assert resp.data == 'server_error'


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
