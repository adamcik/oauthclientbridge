import pytest

from oauthclientbridge import app, compat, crypto, db, errors


@pytest.mark.parametrize(
    'data,expected_error,expected_status',
    [
        ({}, errors.INVALID_CLIENT, 401),
        ({'grant_type': None}, errors.UNSUPPORTED_GRANT_TYPE, 400),
        ({'grant_type': ''}, errors.UNSUPPORTED_GRANT_TYPE, 400),
        (
            {'grant_type': 'authorization_code'},
            errors.UNSUPPORTED_GRANT_TYPE,
            400,
        ),
        ({'client_id': None}, errors.INVALID_CLIENT, 401),
        ({'client_id': ''}, errors.INVALID_CLIENT, 401),
        ({'client_id': ''}, errors.INVALID_CLIENT, 401),
        ({'client_secret': None}, errors.INVALID_CLIENT, 401),
        ({'client_secret': ''}, errors.INVALID_CLIENT, 401),
        ({'client_secret': 'does-not-exist'}, errors.INVALID_CLIENT, 401),
        ({'scope': 'foo'}, errors.INVALID_SCOPE, 400),
        ({'scope': ''}, errors.INVALID_SCOPE, 400),
    ],
)
def test_token_input_validation(post, data, expected_error, expected_status):
    initial = {
        'client_id': 'does-not-exist',
        'client_secret': 'wrong-secret',
        'grant_type': 'client_credentials',
    }

    for key, value in data.items():
        if value is None:
            del initial[key]
        else:
            initial[key] = value

    result, status = post('/token', initial)

    assert status == expected_status
    assert result['error'] == expected_error
    assert result['error_description']


def test_token_invalid_credentials(post, access_token):
    data = {
        'client_id': access_token.client_id,
        'client_secret': 'invalid',
        'grant_type': 'client_credentials',
    }

    result, status = post('/token', data)

    assert status == 401
    assert result['error'] == errors.INVALID_CLIENT
    assert result['error_description']


def test_token_multiple_auth_fails(post, access_token):
    auth = (access_token.client_id, access_token.client_secret)

    data = {
        'client_id': access_token.client_id,
        'client_secret': access_token.client_secret,
        'grant_type': 'client_credentials',
    }

    result, status = post('/token', data, auth=auth)

    assert status == 400
    assert result['error'] == errors.INVALID_REQUEST
    assert result['error_description']


def test_token(post, access_token):
    data = {
        'client_id': access_token.client_id,
        'client_secret': access_token.client_secret,
        'grant_type': 'client_credentials',
    }

    result, status = post('/token', data)

    assert status == 200
    assert access_token.value == result


def test_token_basic_auth(post, access_token):
    auth = (access_token.client_id, access_token.client_secret)

    data = {'grant_type': 'client_credentials'}

    result, status = post('/token', data, auth=auth)

    assert status == 200
    assert access_token.value == result


@pytest.mark.parametrize(
    'base64_basic_auth',
    [
        b'Basic Zm9vOmJhcg==',  # 'foo:bar'
        b'Basic Zm9vOg==',  # 'foo:'
        b'Basic OmJhcg==',  # ':bar'
        b'Basic Og==',  # ':'
        b'Basic ',  # ''
        b'Basic 4zpiYXI=',  # '\xe3o:bar'
        b'Basic 6TpiYXI=',  # \xE9:bar'
        b'Basic ==',  # invalid
        b'Basic xyz',  # invalid
    ],
)
def test_token_bad_basic_auth(post, base64_basic_auth):
    headers = {'Authorization': base64_basic_auth}

    data = {'grant_type': 'client_credentials'}

    result, status = post('/token', data, headers=headers)

    assert status == 401
    assert result['error'] == errors.INVALID_CLIENT


def test_token_wrong_method(client):
    resp = client.get('/token')
    assert resp.status_code == 405


def test_token_revoked(post, access_token):
    db.update(access_token.client_id, None)  # Revoke directly in the db.

    data = {
        'client_id': access_token.client_id,
        'client_secret': access_token.client_secret,
        'grant_type': 'client_credentials',
    }

    result, status = post('/token', data)

    assert status == 400
    assert result['error'] == errors.INVALID_GRANT
    assert result['error_description']


def test_token_wrong_secret_and_not_found_identical(post, access_token):
    data1 = {
        'client_id': access_token.client_id,
        'client_secret': 'bad-secret',
        'grant_type': 'client_credentials',
    }
    data2 = {
        'client_id': 'bad-client',
        'client_secret': access_token.client_secret,
        'grant_type': 'client_credentials',
    }

    result1, status1 = post('/token', data1)
    result2, status2 = post('/token', data2)

    assert status1 == status2
    assert result1 == result2


def test_token_refresh_post_data(post, refresh_token, requests_mock):
    """Test that expected data gets POSTed to provider."""

    def match(request):
        expected = {
            'client_id': [app.config['OAUTH_CLIENT_ID']],
            'client_secret': [app.config['OAUTH_CLIENT_SECRET']],
            'grant_type': ['refresh_token'],
            'refresh_token': [refresh_token.value['refresh_token']],
        }
        assert expected == compat.parse_qs(request.body)
        return True

    requests_mock.post(
        app.config['OAUTH_TOKEN_URI'],
        json={'access_token': 'abc', 'grant_type': 'test'},
        additional_matcher=match,
    )

    data = {
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    }

    post('/token', data)


@pytest.mark.parametrize(
    'response,updated',
    [
        ({}, {}),
        ({'scope': 'foo'}, {}),
        ({'refresh_token': 'def'}, {'refresh_token': 'def'}),
        ({'private': '123'}, {}),
    ],
)
def test_token_with_extra_values(
    post, refresh_token, requests_mock, response, updated
):
    token = {'access_token': 'abc', 'token_type': 'test', 'expires_in': 3600}
    token.update(response)

    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=token)

    data = {
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    }

    post('/token', data)

    expected = refresh_token.value.copy()
    expected.update(updated)

    # Check that the token we fetched got stored directly in db.
    encrypted = db.lookup(refresh_token.client_id)
    actuall = crypto.loads(refresh_token.client_secret, encrypted)

    assert expected == actuall


def test_token_refresh_token_is_not_returned_from_provider(
    post, refresh_token, requests_mock
):
    requests_mock.post(
        app.config['OAUTH_TOKEN_URI'],
        json={
            'access_token': 'abc',
            'token_type': 'test',
            'refresh_token': 'def',
        },
    )

    data = {
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    }

    result, status = post('/token', data)

    expected = {'access_token': 'abc', 'token_type': 'test'}

    assert status == 200
    assert result == expected


def test_token_only_returns_values_from_provider(
    post, refresh_token, requests_mock
):
    token = crypto.dumps(
        refresh_token.client_secret,
        {'refresh_token': 'abc', 'token_type': 'test', 'private': 'foobar'},
    )
    db.update(refresh_token.client_id, token)

    requests_mock.post(
        app.config['OAUTH_TOKEN_URI'],
        json={'access_token': 'abc', 'token_type': 'test'},
    )

    data = {
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    }

    result, status = post('/token', data)

    expected = {'access_token': 'abc', 'token_type': 'test'}

    assert status == 200
    assert result == expected


def test_token_cleans_uneeded_data_from_db(post, refresh_token, requests_mock):
    token = crypto.dumps(
        refresh_token.client_secret,
        {
            'access_token': 'abc',
            'token_type': 'test',
            'refresh_token': 'abc',
            'expires_in': 3600,
        },
    )
    db.update(refresh_token.client_id, token)

    requests_mock.post(
        app.config['OAUTH_TOKEN_URI'],
        json={'access_token': 'abc', 'token_type': 'test'},
    )

    data = {
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    }

    post('/token', data)

    expected = {'refresh_token': 'abc'}

    # Check that the token we fetched got stored directly in db.
    encrypted = db.lookup(refresh_token.client_id)
    actuall = crypto.loads(refresh_token.client_secret, encrypted)

    assert expected == actuall


def test_token_only_returns_scope_from_db(post, refresh_token, requests_mock):
    token = crypto.dumps(
        refresh_token.client_secret,
        {'refresh_token': 'abc', 'token_type': 'test', 'scope': 'foobar'},
    )
    db.update(refresh_token.client_id, token)

    requests_mock.post(
        app.config['OAUTH_TOKEN_URI'],
        json={'access_token': 'abc', 'token_type': 'test'},
    )

    data = {
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    }

    result, status = post('/token', data)

    expected = {'access_token': 'abc', 'token_type': 'test', 'scope': 'foobar'}

    assert status == 200
    assert result == expected


# TODO: fix expected_error and expected_status
@pytest.mark.parametrize(
    'error,expected_error,expected_status',
    [
        (errors.INVALID_REQUEST, errors.INVALID_REQUEST, 400),
        (errors.INVALID_CLIENT, errors.INVALID_CLIENT, 401),
        (errors.INVALID_GRANT, errors.INVALID_GRANT, 400),
        (errors.UNAUTHORIZED_CLIENT, errors.UNAUTHORIZED_CLIENT, 400),
        (errors.UNSUPPORTED_GRANT_TYPE, errors.UNSUPPORTED_GRANT_TYPE, 400),
        (errors.INVALID_SCOPE, errors.INVALID_SCOPE, 400),
        ('errorTransient', errors.TEMPORARILY_UNAVAILABLE, 400),
        ('badError', errors.SERVER_ERROR, 400),
    ],
)
def test_token_provider_errors(
    post, refresh_token, requests_mock, error, expected_error, expected_status
):
    requests_mock.post(
        app.config['OAUTH_TOKEN_URI'], status_code=400, json={'error': error}
    )

    data = {
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    }

    result, status = post('/token', data)

    assert status == expected_status
    assert result['error'] == expected_error
    assert result['error_description']


@pytest.mark.parametrize(
    'token', [{}, {'access_token': 'abc'}, {'token_type': 'test'}]
)
def test_token_provider_invalid_response(
    post, refresh_token, requests_mock, token
):
    requests_mock.post(app.config['OAUTH_TOKEN_URI'], json=token)

    data = {
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    }

    result, status = post('/token', data)

    assert status == 400
    assert result['error'] == errors.INVALID_REQUEST
    assert result['error_description']


def test_token_provider_unavailable(post, refresh_token, requests_mock):
    requests_mock.post(
        app.config['OAUTH_TOKEN_URI'], status_code=503, text='Unavailable.'
    )

    data = {
        'client_id': refresh_token.client_id,
        'client_secret': refresh_token.client_secret,
        'grant_type': 'client_credentials',
    }

    result, status = post('/token', data)

    assert status == 400  # TODO: Make this a 503?
    assert result['error'] == errors.TEMPORARILY_UNAVAILABLE
    assert result['error_description']


# TODO: Test other than basic auth...
# TODO: Test oauth helpers directly?
