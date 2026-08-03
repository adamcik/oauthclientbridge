import functools
from dataclasses import dataclass
from http import HTTPStatus

from oauthclientbridge import types
from oauthclientbridge.utils.bucket import Bucket

RetryAttemptKind = types.RetryAttemptKind
RetryDecisionAction = types.RetryDecisionAction
RetryCondition = types.RetryCondition


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
