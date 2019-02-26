import base64

try:
    from urllib import parse as urlparse
except ImportError:
    import urlparse

import pytest

from oauthclientbridge import app, crypto, db
from oauthclientbridge.errors import *


@pytest.mark.parametrize('data,expected_error,expected_status', [
    ({}, INVALID_CLIENT, 401),
    ({'grant_type': None}, UNSUPPORTED_GRANT_TYPE, 400),
    ({'grant_type': ''}, UNSUPPORTED_GRANT_TYPE, 400),
    ({'grant_type': 'authorization_code'}, UNSUPPORTED_GRANT_TYPE, 400),
    ({'client_id': None}, INVALID_CLIENT, 401),
    ({'client_id': ''}, INVALID_CLIENT, 401),
    ({'client_id': ''}, INVALID_CLIENT, 401),
    ({'client_secret': None}, INVALID_CLIENT, 401),
    ({'client_secret': ''}, INVALID_CLIENT, 401),
    ({'client_secret': 'does-not-exist'}, INVALID_CLIENT, 401),
    ({'scope': 'foo'}, INVALID_SCOPE, 400),
    ({'scope': ''}, INVALID_SCOPE, 400),
])
def test_token_input_validation(post, data, expected_error, expected_status):
    initial = {
        'client_id': 'does-not-exist',
        'client_secret': 'wrong-secret',
        'grant_type': 'client_credentials'
    }

    for key, value in data.items():
        if value is None:
            del initial[key]
        else:
            initial[key] = value

    result, status = post('/token', data=initial)

    assert status == expected_status
    assert result['error'] == expected_error
    assert result['error_description']


def test_token_invalid_credentials(post, access_token):
    result, status = post('/token', data={
        'client_id': access_token.client_id,
        'client_secret': 'invalid',
        'grant_type': 'client_credentials',
    })

    assert status == 401
    assert result['error'] == INVALID_CLIENT
    assert result['error_description']


def test_token_multiple_auth_fails(post, access_token):
    auth = (access_token.client_id, access_token.client_secret)

    result, status = post('/token', auth=auth, data={
        'client_id': access_token.client_id,
        'client_secret': access_token.client_secret,
        'grant_type': 'client_credentials',
    })

    assert status == 400
    assert result['error'] == INVALID_REQUEST
    assert result['error_description']


def test_token(post, access_token):
    result, status = post('/token', data={
        'client_id': access_token.client_id,
        'client_secret': access_token.client_secret,
        'grant_type': 'client_credentials',
    })

    assert status == 200
    assert access_token.value == result


def test_token_basic_auth(post, access_token):
    auth = (access_token.client_id, access_token.client_secret)

    result, status = post('/token', auth=auth, data={
        'grant_type': 'client_credentials',
    })

    assert status == 200
    assert access_token.value == result


def test_token_wrong_method(client):
    resp = client.get('/token')
    assert resp.status_code == 405


def test_token_revoked(post, access_token):
    db.update(access_token.client_id, None)  # Revoke directly in the db.

    result, status = post('/token', data={
        'client_id': access_token.client_id,
        'client_secret': access_token.client_secret,
        'grant_type': 'client_credentials'
    })

    assert status == 400
    assert result['error'] == INVALID_GRANT
    assert result['error_description']


def test_token_wrong_secret_and_not_found_identical(post, access_token):
    result1, status1 = post('/token', data={
        'client_id': access_token.client_id,
        'client_secret': 'bad-secret',
        'grant_type': 'client_credentials'
    })

    result2, status2 = post('/token', data={
        'client_id': 'bad-client',
        'client_secret': access_token.client_secret,
        'grant_type': 'client_credentials'
    })

    assert status1 == status2
    assert result1 == result2


def test_token_refresh_post_data(post, refresh_token, requests_mock):
    """Test that expected data gets POSTed to provider."""

    def match(request):
        parts = request.headers['Authorization'].split(' ')
        assert parts[0] == 'Basic'

        user, password = base64.b64decode(parts[1]).decode('utf-8').split(':')

        assert user == app.config['OAUTH_CLIENT_ID']
        assert password == app.config['OAUTH_CLIENT_SECRET']

        expected = {
            'grant_type': ['refresh_token'],
            'refresh_token': [refresh_token.value['refresh_token']],
        }
        assert expected == urlparse.parse_qs(request.body)
        return True

    requests_mock.post(app.config['OAUTH_TOKEN_URI'],
                       json={'access_token': 'abc', 'grant_type': 'test'},
                       additional_matcher=match)

    post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })


@pytest.mark.parametrize('response,updated', [
    ({}, {}),
    ({'scope': 'foo'}, {}),
    ({'refresh_token': 'def'}, {'refresh_token': 'def'}),
    ({'private': '123'}, {}),
])
def test_token_with_extra_values(
        post, refresh_token, requests_mock, response, updated):
    token = {'access_token': 'abc', 'token_type': 'test', 'expires_in': 3600}
    token.update(response)

    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=token)

    post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })

    expected = refresh_token.value.copy()
    expected.update(updated)

    # Check that the token we fetched got stored directly in db.
    encrypted = db.lookup(refresh_token.client_id)
    actuall = crypto.loads(refresh_token.client_secret, encrypted)

    assert expected == actuall


def test_token_refresh_token_is_not_returned_from_provider(
        post, refresh_token, requests_mock):
    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json={
        'access_token': 'abc',
        'token_type': 'test',
        'refresh_token': 'def',
    })

    result, status = post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })

    expected = {'access_token': 'abc', 'token_type': 'test'}

    assert status == 200
    assert result == expected


def test_token_only_returns_values_from_provider(
        post, refresh_token, requests_mock):
    token = crypto.dumps(refresh_token.client_secret, {
        'refresh_token': 'abc', 'token_type': 'test', 'private': 'foobar',
    })
    db.update(refresh_token.client_id, token)

    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json={
        'access_token': 'abc', 'token_type': 'test',
    })

    result, status = post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })

    expected = {'access_token': 'abc', 'token_type': 'test'}

    assert status == 200
    assert result == expected



def test_token_cleans_uneeded_data_from_db(
        post, refresh_token, requests_mock):
    token = crypto.dumps(refresh_token.client_secret, {
        'access_token': 'abc',
        'token_type': 'test',
        'refresh_token': 'abc',
        'expires_in': 3600,
    })
    db.update(refresh_token.client_id, token)

    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json={
        'access_token': 'abc', 'token_type': 'test',
    })

    post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })

    expected = {'refresh_token': 'abc'}

    # Check that the token we fetched got stored directly in db.
    encrypted = db.lookup(refresh_token.client_id)
    actuall = crypto.loads(refresh_token.client_secret, encrypted)

    assert expected == actuall


def test_token_only_returns_scope_from_db(
        post, refresh_token, requests_mock):
    token = crypto.dumps(refresh_token.client_secret, {
        'refresh_token': 'abc', 'token_type': 'test', 'scope': 'foobar',
    })
    db.update(refresh_token.client_id, token)

    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json={
        'access_token': 'abc', 'token_type': 'test',
    })

    result, status = post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })

    expected = {'access_token': 'abc', 'token_type': 'test', 'scope': 'foobar'}

    assert status == 200
    assert result == expected


# TODO: fix expected_error and expected_status
@pytest.mark.parametrize('error,expected_error,expected_status', [
    (INVALID_REQUEST, INVALID_REQUEST, 400),
    (INVALID_CLIENT, INVALID_CLIENT, 401),
    (INVALID_GRANT, INVALID_GRANT, 400),
    (UNAUTHORIZED_CLIENT, UNAUTHORIZED_CLIENT, 400),
    (UNSUPPORTED_GRANT_TYPE, UNSUPPORTED_GRANT_TYPE, 400),
    (INVALID_SCOPE, INVALID_SCOPE, 400),
    ('errorTransient', TEMPORARILY_UNAVAILABLE, 400),
    ('badError', SERVER_ERROR, 400),
])
def test_token_provider_errors(post, refresh_token, requests_mock,
                               error, expected_error, expected_status):
    requests_mock.post(
        app.config['OAUTH_TOKEN_URI'], status_code=400, json={'error': error})

    result, status = post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })

    assert status == expected_status
    assert result['error'] == expected_error
    assert result['error_description']


@pytest.mark.parametrize('token', [
    {}, {'access_token': 'abc'}, {'token_type': 'test'},
])
def test_token_provider_invalid_response(
        post, refresh_token, requests_mock, token):
    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=token)

    result, status = post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })

    assert status == 400
    assert result['error'] == INVALID_REQUEST
    assert result['error_description']


def test_token_provider_unavailable(post, refresh_token, requests_mock):
    requests_mock.post(
        app.config['OAUTH_TOKEN_URI'], status_code=503, text='Unavailable.')

    result, status = post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })

    assert status == 400  # TODO: Make this a 503?
    assert result['error'] == TEMPORARILY_UNAVAILABLE
    assert result['error_description']


# TODO: Test other than basic auth...
# TODO: Test oauth helpers directly?

