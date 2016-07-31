import json

from cryptography import fernet

InvalidToken = fernet.InvalidToken


def generate_key():
    """Cryptographically safe key that is human readable."""
    return fernet.Fernet.generate_key()


def dumps(key, data):
    """Calls json.dumps on data and encrypts the result with given key."""
    f = fernet.Fernet(bytes(key))
    return f.encrypt(json.dumps(data))


def loads(key, token):
    """Decrypts and verifies token with given key and calls json.loads."""
    f = fernet.Fernet(bytes(key))
    return json.loads(f.decrypt(bytes(token)))
