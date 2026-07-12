import pytest

from oauthclientbridge import crypto


def test_loads_rejects_client_secret_with_impossible_base64_padding():
    token = crypto.dumps(crypto.generate_key(), {"access_token": "123"})

    with pytest.raises(crypto.InvalidToken):
        _ = crypto.loads("a", token)
