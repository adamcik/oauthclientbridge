import importlib.metadata
from http import HTTPStatus

import anyio
import httpx

from oauthclientbridge import types
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import FetchSettings

from ._core import jitter_delay, parse_retry
from ._outcome import OAuthResponse, token_endpoint_outcome


class HttpxFetcher:
    def __init__(self, settings: FetchSettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=settings.pool_max_connections,
                max_keepalive_connections=settings.pool_max_keepalive_connections,
                keepalive_expiry=settings.pool_keepalive_expiry,
            ),
            timeout=httpx.Timeout(
                connect=settings.timeout,
                read=settings.timeout,
                write=settings.timeout,
                pool=settings.pool_timeout,
            ),
        )

    async def __call__(
        self,
        uri: str,
        upstream_grant_type: types.UpstreamGrantType,
        auth: str | None = None,
        **data: str | None,
    ) -> OAuthResponse:
        return await _fetch(
            self._client, self._settings, uri, upstream_grant_type, data, auth
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def create_fetcher(settings: FetchSettings) -> HttpxFetcher:
    return HttpxFetcher(settings)


async def _fetch(
    client: httpx.AsyncClient,
    settings: FetchSettings,
    uri: str,
    upstream_grant_type: types.UpstreamGrantType,
    data: dict[str, str | None],
    auth: str | None = None,
) -> OAuthResponse:
    _ = upstream_grant_type
    timed_out_result: OAuthResponse | None = None
    try:
        with anyio.fail_after(settings.total_timeout):
            for attempt in range(settings.total_retries + 1):
                retry_after = 0
                status: HTTPStatus | None = None
                try:
                    headers = {
                        "User-Agent": "oauthclientbridge %s"
                        % importlib.metadata.version("oauthclientbridge")
                    }
                    if auth is None:
                        response = await client.post(uri, data=data, headers=headers)
                    else:
                        response = await client.post(
                            uri,
                            data=data,
                            auth=httpx.BasicAuth(auth, ""),
                            headers=headers,
                        )
                except httpx.TimeoutException:
                    result = OAuthError.SERVER_ERROR.json(
                        description="Request timed out while connecting to provider."
                    )
                except httpx.HTTPError:
                    result = OAuthError.SERVER_ERROR.json(
                        description="An error occurred while connecting to the provider."
                    )
                else:
                    status = HTTPStatus(response.status_code)
                    retry_after = parse_retry(response.headers.get("retry-after"))
                    if response.is_redirect:
                        result = OAuthError.SERVER_ERROR.json(
                            description="Unhandled provider error (HTTP %s)."
                            % response.status_code
                        )
                    else:
                        try:
                            result = response.json()
                        except ValueError:
                            if status in settings.unavailable_status_codes:
                                result = OAuthError.TEMPORARILY_UNAVAILABLE.json(
                                    description="Provider is unavailable."
                                )
                            else:
                                result = OAuthError.SERVER_ERROR.json(
                                    description="Unhandled provider error (HTTP %s)."
                                    % response.status_code
                                )

                outcome = token_endpoint_outcome(
                    status,
                    result,
                    retry_status_codes=settings.retry_status_codes,
                    error_types=settings.error_types,
                )
                if not outcome.retryable:
                    if (
                        status is not None
                        and status.is_success
                        and outcome.normalized_error is None
                    ):
                        return result

                    description = result.get("error_description")
                    return (outcome.normalized_error or OAuthError.SERVER_ERROR).json(
                        description=description
                        if isinstance(description, str)
                        else None
                    )

                description = result.get("error_description")
                result = (outcome.normalized_error or OAuthError.SERVER_ERROR).json(
                    description=description if isinstance(description, str) else None
                )
                if retry_after:
                    result["retry_after"] = retry_after
                if attempt == settings.total_retries:
                    return result

                timed_out_result = result

                delay = (
                    retry_after or (2 ** (attempt + 1) - 1) * settings.backoff_factor
                )
                if delay:
                    sleep_for = jitter_delay(
                        delay, settings, preserve_floor=retry_after > 0
                    )
                    if retry_after:
                        sleep_for = max(retry_after, sleep_for)
                    await anyio.sleep(sleep_for)
    except TimeoutError:
        if timed_out_result is not None:
            return timed_out_result
        return OAuthError.SERVER_ERROR.json(
            description="Request timed out while connecting to provider."
        )

    return OAuthError.SERVER_ERROR.json(
        description="An unknown error occurred while talking to provider."
    )
