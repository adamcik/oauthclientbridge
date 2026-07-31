import importlib.metadata

import anyio
import httpx

from oauthclientbridge import types
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import FetchSettings

from ._outcome import OAuthResponse


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
    try:
        with anyio.fail_after(settings.total_timeout):
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
    except TimeoutError:
        return OAuthError.SERVER_ERROR.json(
            description="Request timed out while connecting to provider."
        )
    except httpx.HTTPError:
        return OAuthError.SERVER_ERROR.json(
            description="An error occurred while connecting to the provider."
        )

    if response.is_redirect:
        return OAuthError.SERVER_ERROR.json(
            description="Unhandled provider error (HTTP %s)." % response.status_code
        )
    try:
        return response.json()
    except ValueError:
        pass
    if response.status_code in settings.unavailable_status_codes:
        return OAuthError.TEMPORARILY_UNAVAILABLE.json(
            description="Provider is unavailable."
        )
    return OAuthError.SERVER_ERROR.json(
        description="Unhandled provider error (HTTP %s)." % response.status_code
    )
