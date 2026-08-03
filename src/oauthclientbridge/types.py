import uuid
from enum import StrEnum
from typing import NewType

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonDict = dict[str, JsonValue]

ClientId = NewType("ClientId", uuid.UUID)
ClientSecret = NewType("ClientSecret", str)
EncryptedToken = NewType("EncryptedToken", bytes)


class Endpoint(StrEnum):
    AUTHORIZE = "authorize"
    CALLBACK = "callback"
    TOKEN = "token"
    METRICS = "metrics"
    UNKNOWN = "unknown"


class DatabaseOperation(StrEnum):
    """Bounded database operations used as telemetry labels."""

    CHECK_TOKENS_TABLE = "check_tokens_table"
    INSERT_TOKEN = "insert_token"
    LOOKUP_TOKEN = "lookup_token"
    UPDATE_TOKEN = "update_token"
    COUNT_TOKEN_STATES = "count_token_states"


class DatabaseError(StrEnum):
    """Bounded DB-API error categories used as telemetry labels."""

    ERROR = "error"
    INTERFACE_ERROR = "interface_error"
    DATABASE_ERROR = "database_error"
    DATA_ERROR = "data_error"
    OPERATIONAL_ERROR = "operational_error"
    INTEGRITY_ERROR = "integrity_error"
    INTERNAL_ERROR = "internal_error"
    PROGRAMMING_ERROR = "programming_error"
    NOT_SUPPORTED_ERROR = "not_supported_error"


class UpstreamGrantType(StrEnum):
    # TODO: migrate OAuth client metric labels to upstream grant types.
    AUTHORIZATION_CODE = "token"
    REFRESH_TOKEN = "refresh"


class RetryAttemptKind(StrEnum):
    """Bounded kinds of attempts made during an upstream fetch."""

    INITIAL = "initial"
    RETRY = "retry"


class RetryDecisionAction(StrEnum):
    """Bounded actions taken after a retry decision."""

    RETRY = "retry"
    SKIP = "skip"


class RetryCondition(StrEnum):
    """Bounded conditions that cause a retry decision."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    UNAVAILABLE = "unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    UNKNOWN = "unknown"
