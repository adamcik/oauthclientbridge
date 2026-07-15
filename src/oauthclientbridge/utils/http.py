import enum
from http import HTTPStatus


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
