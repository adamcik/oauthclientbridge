import json

from cryptography import fernet

from typing import Any

InvalidToken = fernet.InvalidToken


def generate_key() -> str:
    """Cryptographically safe key that is human readable."""
    return fernet.Fernet.generate_key().decode("ascii")


def dumps(key: str, data: dict[str, Any]) -> bytes:
    """Calls json.dumps on data and encrypts the result with given key."""
    f = fernet.Fernet(key.encode("ascii"))
    return f.encrypt(json.dumps(data).encode("utf-8"))


def loads(key: str, token: bytes) -> dict[str, Any]:
    """Decrypts and verifies token with given key and calls json.loads."""
    f = fernet.Fernet(key.encode("ascii"))
    return json.loads(f.decrypt(token).decode("utf-8"))
