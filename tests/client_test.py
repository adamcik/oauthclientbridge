import uuid
from dataclasses import dataclass

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


@dataclass(frozen=True, kw_only=True)
class ValidateCredentialsCase:
    name: str
    client_id: str | None
    client_secret: str | None
    error_type: type[ValueError]
    message: str


@pytest.mark.parametrize(
    "case",
    [
        ValidateCredentialsCase(
            name="missing client id",
            client_id=None,
            client_secret="secret",
            error_type=client.CredentialValidationError,
            message="client_id must be set",
        ),
        ValidateCredentialsCase(
            name="empty client id",
            client_id="",
            client_secret="secret",
            error_type=client.CredentialValidationError,
            message="client_id must be set",
        ),
        ValidateCredentialsCase(
            name="missing client secret",
            client_id="00000000-0000-0000-0000-000000000001",
            client_secret=None,
            error_type=client.CredentialValidationError,
            message="client_secret must be set",
        ),
        ValidateCredentialsCase(
            name="empty client secret",
            client_id="00000000-0000-0000-0000-000000000001",
            client_secret="",
            error_type=client.CredentialValidationError,
            message="client_secret must be set",
        ),
        ValidateCredentialsCase(
            name="same id and secret",
            client_id="same",
            client_secret="same",
            error_type=client.CredentialValidationError,
            message="set to same value",
        ),
        ValidateCredentialsCase(
            name="malformed client id",
            client_id="malformed",
            client_secret="secret",
            error_type=client.ClientIdValidationError,
            message="Malformed client_id",
        ),
        ValidateCredentialsCase(
            name="malformed client secret",
            client_id="00000000-0000-0000-0000-000000000001",
            client_secret="not-a-fernet-key",
            error_type=client.ClientSecretValidationError,
            message="Malformed client_secret",
        ),
    ],
    ids=lambda case: case.name,
)
def test_validate_credentials_rejects_invalid_values(
    case: ValidateCredentialsCase,
) -> None:
    with pytest.raises(case.error_type, match=case.message):
        _ = client.validate_credentials(case.client_id, case.client_secret)
