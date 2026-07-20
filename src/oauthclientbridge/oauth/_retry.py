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


class RetryReason(StrEnum):
    UNAVAILABLE = "unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RetryDecision:
    action: RetryDecisionAction
    reason: RetryReason


@functools.lru_cache()
def get_retry_limiter(capacity: int, refill_per_initial: float) -> Bucket:
    """Process-local retry budget.

    We model this as a bounded bucket of retry tokens. First attempts replenish
    the bucket by a configured fraction, while each admitted retry consumes one
    whole token. This keeps the implementation local and simple, but it means
    the budget is per-process and only approximates fleet-wide retry volume.
    """
    return Bucket(capacity, refill_per_initial)


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
