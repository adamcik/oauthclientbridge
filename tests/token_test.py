import urlparse

import pytest

from oauthclientbridge import app, crypto, db


@pytest.mark.parametrize('data,expected_error,expected_status', [
    ({}, 'invalid_client', 401),
    ({'grant_type': None}, 'unsupported_grant_type', 400),
    ({'grant_type': ''}, 'unsupported_grant_type', 400),
    ({'grant_type': 'authorization_code'}, 'unsupported_grant_type', 400),
    ({'client_id': None}, 'invalid_client', 401),
    ({'client_id': ''}, 'invalid_client', 401),
    ({'client_id': ''}, 'invalid_client', 401),
    ({'client_secret': None}, 'invalid_client', 401),
    ({'client_secret': ''}, 'invalid_client', 401),
    ({'client_secret': 'does-not-exist'}, 'invalid_client', 401),
    ({'scope': 'foo'}, 'invalid_scope', 400),
    ({'scope': ''}, 'invalid_scope', 400),
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
    assert result['error'] == 'invalid_client'
    assert result['error_description']


def test_token_multiple_auth_fails(post, access_token):
    auth = (access_token.client_id, access_token.client_secret)

    result, status = post('/token', auth=auth, data={
        'client_id': access_token.client_id,
        'client_secret': access_token.client_secret,
        'grant_type': 'client_credentials',
    })

    assert status == 400
    assert result['error'] == 'invalid_request'
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
    assert result['error'] == 'invalid_grant'
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
        auth_header = request.headers.get('Authorization', '')
        assert auth_header.startswith('Basic ')

        user, password = auth_header[6:].decode('base64').split(':')
        assert user == app.config['OAUTH_CLIENT_ID']
        assert password == app.config['OAUTH_CLIENT_SECRET']

        expected = {
            'grant_type': ['refresh_token'],
            'refresh_token': [refresh_token.value['refresh_token']],
        }
        assert expected == urlparse.parse_qs(request.body)
        return True

    requests_mock.post(app.config['OAUTH_TOKEN_URI'],
                       json=refresh_token.value,
                       additional_matcher=match)

    post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })


def test_token_with_refresh_token(post, refresh_token, requests_mock):
    new_token = refresh_token.value.copy()
    new_token['refresh_token'] = 'def'

    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=new_token)

    post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })

    # Check that the token we fetched got stored directly in db.
    encrypted_token = db.lookup(refresh_token.client_id)
    stored_token = crypto.loads(refresh_token.client_secret, encrypted_token)

    assert new_token == stored_token


def test_token_removes_refresh_token(post, refresh_token, requests_mock):
    expected_token = refresh_token.value.copy()
    del expected_token['refresh_token']

    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=refresh_token.value)

    result, status = post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })

    assert status == 200
    assert result == expected_token


# TODO: fix expected_error and expected_status
@pytest.mark.parametrize('error,expected_error,expected_status', [
    ('invalid_request', 'invalid_request', 400),
    ('invalid_client', 'invalid_client', 401),
    ('invalid_grant', 'invalid_grant', 400),
    ('unauthorized_client', 'unauthorized_client', 400),
    ('unsupported_grant_type', 'unsupported_grant_type', 400),
    ('invalid_scope', 'invalid_scope', 400),
    ('errorTransient', 'temporarily_unavailable', 400),
    ('badError', 'server_error', 400),
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


@pytest.mark.parametrize('field', ['access_token', 'token_type'])
def test_token_provider_invalid_response(post, refresh_token, requests_mock,
                                         field):
    token = refresh_token.value.copy()
    del token[field]

    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=token)

    result, status = post('/token', data={
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    })

    assert status == 400
    assert result['error'] == 'invalid_request'
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
    assert result['error'] == 'temporarily_unavailable'
    assert result['error_description']


# TODO: Test other than basic auth...
# TODO: Test oauth helpers directly?

