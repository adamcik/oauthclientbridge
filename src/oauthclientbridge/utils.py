import enum
import urllib.parse
from http import HTTPStatus

URIParam = dict[str, str]


class APIResult(enum.StrEnum):
    SUCCESS = "success"
    CLIENT_ERROR = "client_error"
    SERVER_ERROR = "server_error"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


def http_status_to_result(status: HTTPStatus) -> APIResult:
    if status.is_success:
        return APIResult.SUCCESS
    elif status.is_redirection:
        # Redirects are not followed by our client, so we treat them as an
        # unexpected response, which is a form of client error.
        return APIResult.CLIENT_ERROR
    elif status == HTTPStatus.TOO_MANY_REQUESTS:
        return APIResult.RATE_LIMITED
    elif status.is_client_error:
        return APIResult.CLIENT_ERROR
    elif status.is_server_error:
        return APIResult.SERVER_ERROR
    else:
        return APIResult.UNKNOWN


def _rewrite_query(original: str, params: URIParam) -> str:
    parts = []
    query = urllib.parse.parse_qs(original, keep_blank_values=True)
    for p, value in params.items():
        query[p] = [value]  # Override with new params.
    for q, values in query.items():
        for value in values:  # Turn query into list of tuples.
            if isinstance(value, str):
                parts.append((q, value.encode("utf-8")))
            else:
                parts.append((q, value))
    return urllib.parse.urlencode(parts)


def rewrite_uri(uri: str, params: URIParam) -> str:
    scheme, netloc, path, query, fragment = urllib.parse.urlsplit(uri)
    query = _rewrite_query(query, params)
    return urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))
