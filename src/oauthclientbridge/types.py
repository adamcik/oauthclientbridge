import uuid
from typing import NewType

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonDict = dict[str, JsonValue]

ClientId = NewType("ClientId", uuid.UUID)
ClientSecret = NewType("ClientSecret", str)
EncryptedToken = NewType("EncryptedToken", bytes)
