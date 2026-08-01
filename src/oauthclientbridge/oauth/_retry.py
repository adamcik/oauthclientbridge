import functools
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus

from oauthclientbridge.utils.bucket import Bucket


class RetryAttemptKind(StrEnum):
    INITIAL = "initial"
    RETRY = "retry"


class RetryDecisionAction(StrEnum):
    RETRY = "retry"
    SKIP = "skip"


class RetryCondition(StrEnum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    UNAVAILABLE = "unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RetryDecision:
    action: RetryDecisionAction
    reason: RetryCondition


@functools.lru_cache()
def get_retry_limiter(capacity: int, refill_per_initial: float) -> Bucket:
    """Return the process-local retry budget for matching settings."""
    return Bucket(capacity, refill_per_initial)


def retry_condition_for_status(status: HTTPStatus) -> RetryCondition:
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return RetryCondition.RESOURCE_EXHAUSTED
    if status in {
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }:
        return RetryCondition.UNAVAILABLE
    return RetryCondition.UNKNOWN
