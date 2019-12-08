import json
import typing

from cryptography import fernet

if typing.TYPE_CHECKING:
    from typing import Any, Dict, Text, Union  # noqa: F401


InvalidToken = fernet.InvalidToken


def generate_key():  # type: () -> Text
    """Cryptographically safe key that is human readable."""
    return fernet.Fernet.generate_key().decode('ascii')


def dumps(
    key, data
):  # type: (Text, Dict[str, Union[str, int, float]]) -> bytes
    """Calls json.dumps on data and encrypts the result with given key."""
    f = fernet.Fernet(key.encode('ascii'))
    return f.encrypt(json.dumps(data).encode('utf-8'))


def loads(
    key, token
):  # type: (Text, bytes) -> Dict[str, Union[str, int, float]]
    """Decrypts and verifies token with given key and calls json.loads."""
    f = fernet.Fernet(key.encode('ascii'))
    return json.loads(f.decrypt(token).decode('utf-8'))
