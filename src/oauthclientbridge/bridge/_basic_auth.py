import base64
import binascii


def credentials(authorization: str | None) -> tuple[str, str] | None:
    if authorization is None:
        return None
    scheme, separator, encoded_credentials = authorization.partition(" ")
    if scheme.casefold() != "basic" or not separator:
        return None
    try:
        decoded_credentials = base64.b64decode(
            encoded_credentials, validate=True
        ).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return ("", "")
    username, separator, password = decoded_credentials.partition(":")
    if not separator:
        return ("", "")
    return username, password
