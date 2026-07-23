from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus


class RetryAttemptKind(StrEnum):
    INITIAL = "initial"
    RETRY = "retry"


class RetryDecisionAction(StrEnum):
    RETRY = "retry"
    SKIP = "skip"


class RetryReason(StrEnum):
    UNAVAILABLE = "unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RetryDecision:
    action: RetryDecisionAction
    reason: RetryReason


def retry_reason_for_status(status: HTTPStatus) -> RetryReason:
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return RetryReason.RESOURCE_EXHAUSTED
    if status in {
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }:
        return RetryReason.UNAVAILABLE
    return RetryReason.UNKNOWN
