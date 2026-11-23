import email.utils
import random
import re
import time
from http import HTTPStatus
from typing import Any, Protocol, override

from opentelemetry import metrics, trace

from oauthclientbridge import telemetry, types
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import FetchSettings

from ._outcome import OAuthResponse, UpstreamResult, upstream_result_for_status

_otel_scope = __package__ or __name__
meter = metrics.get_meter(_otel_scope)
REDACTED_LOG_VALUE = "<REDACTED>"
ALLOWED_LOG_FIELDS = frozenset(
    {"client_id", "error", "error_description", "error_uri", "retry_after"}
)

_oauth_client_total_counter = meter.create_counter(
    name="oauth.client.total",
    description="Measures the total number of OAuth client requests.",
)

_oauth_client_duration_histogram = meter.create_histogram(
    name="oauth.client.duration",
    description="Measures the duration of an OAuth client request, including retries.",
    unit="s",
)

_oauth_client_retries_histogram = meter.create_histogram(
    name="oauth.client.retries",
    description="Measures the number of retries for an OAuth client request.",
    unit="1",
)

URIParam = dict[str, str]


class UpstreamFetcher(Protocol):
    async def __call__(
        self,
        uri: str,
        upstream_grant_type: types.UpstreamGrantType,
        auth: str | None = None,
        **data: str | None,
    ) -> OAuthResponse: ...


class Error(Exception):
    def __init__(
        self,
        error: OAuthError,
        description: str | None = None,
        uri: str | None = None,
        retry_after: int | None = None,
    ):
        super().__init__()
        self.error: OAuthError = error
        self.description: str = description or error.description
        self.uri: str | None = uri
        self.retry_after: int | None = retry_after

    @override
    def __str__(self) -> str:
        return f"{self.error}: {self.description}"


def scrub_refresh_token(token: OAuthResponse) -> OAuthResponse:
    if "refresh_token" not in token:
        return token
    remove = ("access_token", "expires_in", "token_type")
    return {k: v for k, v in token.items() if k not in remove}


def sanitize_for_logging(payload: OAuthResponse) -> OAuthResponse:
    return {
        key: value if key in ALLOWED_LOG_FIELDS else REDACTED_LOG_VALUE
        for key, value in payload.items()
    }


def record_fetch_metrics(
    span: trace.Span,
    endpoint: types.UpstreamGrantType,
    status: HTTPStatus | None,
    result: OAuthResponse,
    completed_retries: int,
    duration: float,
) -> None:
    final_result = (
        UpstreamResult.TIMEOUT if status is None else upstream_result_for_status(status)
    )
    attributes: dict[str, Any] = {
        "operation": endpoint,
        "final.result": final_result,
    }
    if status:
        attributes["http.response.status_code"] = int(status)

    telemetry.record_client_retries_metric(endpoint, status, completed_retries)

    error_type = result.get("error")
    if error_type:
        attributes["error.type"] = error_type
        span.set_status(trace.Status(trace.StatusCode.ERROR, str(result)))

    span.set_attribute("total_retries", completed_retries)
    for key, value in attributes.items():
        span.set_attribute(key, value)

    _oauth_client_duration_histogram.record(duration, attributes)
    _oauth_client_retries_histogram.record(completed_retries, attributes)
    _oauth_client_total_counter.add(1, attributes)


def parse_retry(value: str | None) -> int:
    if not value:
        seconds = 0
    elif re.match(r"^\s*[0-9]+\s*$", value):
        seconds = int(value)
    else:
        parsed = email.utils.parsedate_tz(value)
        if parsed is None:
            seconds = 0
        else:
            seconds = int(email.utils.mktime_tz(parsed) - time.time())
    return max(0, seconds)


def jitter_delay(
    delay: float, settings: FetchSettings, preserve_floor: bool = False
) -> float:
    """Apply jitter around a base delay.

    When preserving a provider-supplied floor like `Retry-After`, clamp the
    lower jitter bound to 1.0 so the full sampled range remains usable.
    """
    lower_bound = settings.backoff_jitter_min
    if preserve_floor:
        lower_bound = max(1.0, lower_bound)

    return delay * random.uniform(
        lower_bound,
        settings.backoff_jitter_max,
    )
