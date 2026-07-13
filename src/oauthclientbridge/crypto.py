import binascii
import json
from typing import Any

from cryptography import fernet

from oauthclientbridge import types

InvalidToken = fernet.InvalidToken


def _normalize_key_padding(key: str) -> str:
    if (remainder := len(key) % 4) in (2, 3):
        return key + ("=" * (4 - remainder))
    return key


def generate_key() -> types.ClientSecret:
    """Cryptographically safe key that is human readable."""
    return types.ClientSecret(fernet.Fernet.generate_key().decode("ascii"))


def validate_key(key: str) -> types.ClientSecret:
    """Validate an encoded Fernet key and mark it as a client secret."""
    try:
        _ = fernet.Fernet(_normalize_key_padding(key).encode("ascii"))
    except (ValueError, binascii.Error) as e:
        raise InvalidToken from e
    return types.ClientSecret(key)


def dumps(key: types.ClientSecret, data: dict[str, Any]) -> types.EncryptedToken:
    """Calls json.dumps on data and encrypts the result with given key."""
    f = fernet.Fernet(key.encode("ascii"))
    return types.EncryptedToken(f.encrypt(json.dumps(data).encode("utf-8")))


def loads(key: types.ClientSecret, token: types.EncryptedToken) -> dict[str, Any]:
    """Decrypts and verifies token with given key and calls json.loads."""
    try:
        f = fernet.Fernet(_normalize_key_padding(key).encode("ascii"))
    except (ValueError, binascii.Error) as e:
        raise InvalidToken from e
    return json.loads(f.decrypt(token).decode("utf-8"))
