"""Pydantic models for validating upstream OAuth responses."""

import time
import typing

import pydantic
from pydantic.types import JsonValue

__all__ = ["ErrorResponse", "TokenResponse"]


class JsonBase(pydantic.BaseModel):
    # Keep any extra fields we might get, but make sure they must be JSON.
    model_config = pydantic.ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, JsonValue]


class ErrorResponse(JsonBase):
    # TODO: Consider `OAuthError | str` to map to enum.
    error: str
    error_description: str | None = None
    error_uri: str | None = None


class TokenResponse(JsonBase):
    # We only expect `bearer`, but the model should not check this.
    token_type: typing.Literal["bearer"] | str

    access_token: pydantic.SecretStr
    refresh_token: pydantic.SecretStr | None = None

    # We don't expect id_token, but treat it as a secret if it shows up.
    id_token: pydantic.SecretStr | None = None

    scope: set[str] | None = None
    """The scope of the access token.

    Represented as a space-separated string in the response and parsed into a
    set of strings or None.
    """

    expires_at: int | None = None
    expires_in: int | None = pydantic.Field(None, gt=0, exclude=True)

    # NOTE: Other fields might be present and are guaranteed to be JsonValues.

    def is_expired(self, leeway: int = 120) -> bool:
        """Helper to check if the token is expired.

        Accounts for clock skew with optional leeway, defaults to 120 seconds.
        """
        if self.expires_at is None:
            return False  # Token does not expire
        return time.time() + leeway > self.expires_at

    @pydantic.model_validator(mode="after")
    def _calculate_expires_at_from_expires_in(self):
        # NOTE: The downside of this approach is that we loose the info
        # regarding if `expires_at` was set or not. But I don't think we need
        # that info for anything.
        if self.expires_in and not self.expires_at:
            self.expires_at = int(time.time() + self.expires_in)
        return self

    @pydantic.field_validator("expires_in", "expires_at", mode="before")
    def _coerce_float_to_int(cls, v: JsonValue) -> JsonValue | int:
        if isinstance(v, float):
            return int(v)
        return v

    @pydantic.field_validator("token_type", mode="before")
    def _normalize_token_type_to_lowercase(cls, v: JsonValue) -> JsonValue | str:
        if isinstance(v, str):
            return v.lower()
        return v

    @pydantic.field_validator("scope", mode="before")
    def _coerce_scope_to_set(cls, v: JsonValue) -> JsonValue | set[str]:
        if isinstance(v, str):
            return set(v.split(" "))
        return v

    @pydantic.field_serializer(
        "access_token", "id_token", "refresh_token", when_used="json"
    )
    def _dump_secret_in_json_mode(self, v: pydantic.SecretStr | None) -> str | None:
        if v is not None:
            return v.get_secret_value()
        return None
