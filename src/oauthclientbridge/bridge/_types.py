from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TypeAlias

from oauthclientbridge.types import JsonDict

Session: TypeAlias = dict[str, str]


@dataclass(frozen=True)
class BridgeResponse:
    status: HTTPStatus
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    body: bytes | JsonDict = b""
    session: Session | None = None
