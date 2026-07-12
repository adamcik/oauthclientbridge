import binascii
import json
from typing import Any

from cryptography import fernet

InvalidToken = fernet.InvalidToken


def _normalize_key_padding(key: str) -> str:
    if (remainder := len(key) % 4) in (2, 3):
        return key + ("=" * (4 - remainder))
    return key


def generate_key() -> str:
    """Cryptographically safe key that is human readable."""
    return fernet.Fernet.generate_key().decode("ascii")


def dumps(key: str, data: dict[str, Any]) -> bytes:
    """Calls json.dumps on data and encrypts the result with given key."""
    f = fernet.Fernet(key.encode("ascii"))
    return f.encrypt(json.dumps(data).encode("utf-8"))


def loads(key: str, token: bytes) -> dict[str, Any]:
    """Decrypts and verifies token with given key and calls json.loads."""
    try:
        f = fernet.Fernet(_normalize_key_padding(key).encode("ascii"))
    except (ValueError, binascii.Error) as e:
        raise InvalidToken from e
    return json.loads(f.decrypt(token).decode("utf-8"))
