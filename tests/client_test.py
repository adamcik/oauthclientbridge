import uuid

import pytest

from oauthclientbridge import client, crypto, types


def test_validate_credentials_returns_typed_credentials() -> None:
    client_secret = crypto.generate_key()
    credentials = client.validate_credentials(
        "00000000-0000-0000-0000-000000000001", client_secret
    )

    assert credentials.client_id == types.ClientId(
        uuid.UUID("00000000-0000-0000-0000-000000000001")
    )
    assert credentials.client_secret == client_secret


@pytest.mark.parametrize(
    ("client_id", "client_secret", "error_type", "message"),
    [
        (None, "secret", client.CredentialValidationError, "client_id must be set"),
        ("", "secret", client.CredentialValidationError, "client_id must be set"),
        (
            "00000000-0000-0000-0000-000000000001",
            None,
            client.CredentialValidationError,
            "client_secret must be set",
        ),
        (
            "00000000-0000-0000-0000-000000000001",
            "",
            client.CredentialValidationError,
            "client_secret must be set",
        ),
        ("same", "same", client.CredentialValidationError, "set to same value"),
        ("malformed", "secret", client.ClientIdValidationError, "Malformed client_id"),
        (
            "00000000-0000-0000-0000-000000000001",
            "not-a-fernet-key",
            client.ClientSecretValidationError,
            "Malformed client_secret",
        ),
    ],
)
def test_validate_credentials_rejects_invalid_values(
    client_id: str | None,
    client_secret: str | None,
    error_type: type[ValueError],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        _ = client.validate_credentials(client_id, client_secret)
