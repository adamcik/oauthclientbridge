import uuid
from typing import NewType

ClientId = NewType("ClientId", uuid.UUID)
ClientSecret = NewType("ClientSecret", str)
EncryptedToken = NewType("EncryptedToken", bytes)
