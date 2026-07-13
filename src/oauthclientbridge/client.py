"""Bridge-issued client credentials, distinct from upstream OAuth handling.

The `oauthclientbridge.oauth` package communicates with the configured OAuth
provider. This module validates the client ID and secret issued by this bridge
to its callers.
"""

from dataclasses import dataclass

from oauthclientbridge import crypto, db, types


@dataclass(frozen=True)
class ClientCredentials:
    client_id: types.ClientId
    client_secret: types.ClientSecret


class CredentialValidationError(ValueError):
    pass


class ClientIdValidationError(CredentialValidationError):
    pass


class ClientSecretValidationError(CredentialValidationError):
    pass


def validate_credentials(
    client_id: str | None,
    client_secret: str | None,
) -> ClientCredentials:
    if client_id is None or client_id == "":
        raise CredentialValidationError("client_id must be set.")

    if client_id == client_secret:
        raise CredentialValidationError(
            "client_id and client_secret set to same value."
        )

    try:
        normalized_client_id = db.validate_client_id(client_id)
    except ValueError as e:
        raise ClientIdValidationError("Malformed client_id.") from e

    if client_secret is None or client_secret == "":
        raise CredentialValidationError("client_secret must be set.")

    try:
        validated_client_secret = crypto.validate_key(client_secret)
    except crypto.InvalidToken as e:
        raise ClientSecretValidationError("Malformed client_secret.") from e

    return ClientCredentials(
        client_id=normalized_client_id,
        client_secret=validated_client_secret,
    )
