import pytest

from oauthclientbridge import crypto


def test_validate_key_accepts_padded_and_unpadded_fernet_keys():
    key = crypto.generate_key()

    assert crypto.validate_key(key) == key
    assert crypto.validate_key(key.rstrip("=")) == key


def test_validate_key_rejects_malformed_key():
    with pytest.raises(crypto.InvalidToken):
        _ = crypto.validate_key("not-a-fernet-key")


def test_loads_rejects_client_secret_with_impossible_base64_padding():
    token = crypto.dumps(crypto.generate_key(), {"access_token": "123"})

    with pytest.raises(crypto.InvalidToken):
        _ = crypto.loads("a", token)
