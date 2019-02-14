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
    ('?state={state}&error=badErrorCode', 'invalid_error'),
])
def test_callback_error_handling(query, expected_error, client, state):
    app.config['OAUTH_CALLBACK_TEMPLATE'] = '{{error}}'

    resp = client.get('/callback' + query.format(state=state))
    assert resp.status_code == 400
    assert resp.data == expected_error


@pytest.mark.parametrize('data,expected_error', [
    ({}, 'server_error'),
    ({'token_type': 'foobar'}, 'server_error'),
    ({'access_token': 'foobar'}, 'server_error'),
    ({'access_token': '', 'token_type': ''}, 'server_error'),
    ({'access_token': 'foobar', 'token_type': ''}, 'server_error'),
    ({'access_token': '', 'token_type': 'foobar'}, 'server_error'),
    ({'error': 'invalid_request'}, 'invalid_request'),
    ({'error': 'invalid_client'}, 'invalid_client'),
    ({'error': 'invalid_grant'}, 'invalid_grant'),
    ({'error': 'unauthorized_client'}, 'unauthorized_client'),
    ({'error': 'unsupported_grant_type'}, 'unsupported_grant_type'),
    ({'error': 'invalid_scope'}, 'invalid_scope'),
    ({'error': 'server_error'}, 'server_error'),
    ({'error': 'temporarily_unavailable'}, 'temporarily_unavailable'),
    ({'error': 'errorTransient'}, 'temporarily_unavailable'),
    ({'error': 'badErrorCode'}, 'invalid_error'),
])
def test_callback_authorization_code_error_handling(
        data, expected_error, client, state, requests_mock):
    app.config['OAUTH_CALLBACK_TEMPLATE'] = '{{error}}'

    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=data)

    resp = client.get('/callback?code=1234&state=' + state)
    assert resp.status_code == 400
    assert resp.data == expected_error


# TODO: Test with more status codes from callback...
def test_callback_authorization_code_invalid_response(
        client, state, requests_mock):
    app.config['OAUTH_CALLBACK_TEMPLATE'] = '{{error}}'

    requests_mock.post(app.config['OAUTH_TOKEN_URI'], text='Not a JSON value')

    resp = client.get('/callback?code=1234&state=' + state)
    assert resp.status_code == 400
    assert resp.data == 'server_error'


def test_callback_authorization_code_store_token(client, state, requests_mock):
    app.config['OAUTH_CALLBACK_TEMPLATE'] = '{{client_id}}:{{client_secret}}'

    data = {'token_type': 'bearer', 'access_token': '1234567890'}
    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=data)

    resp = client.get('/callback?code=1234&state=' + state)
    client_id, client_secret = resp.data.split(':')

    # Peek inside internals to check that our token got stored.
    assert data == crypto.loads(client_secret, db.lookup(client_id))


# TODO: Duplicate client-id handling?
