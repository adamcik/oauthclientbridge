from collections.abc import Mapping
from http import HTTPStatus

import jinja2

from ._types import BridgeResponse

environment = jinja2.Environment(autoescape=True)


def render_template(
    template: str,
    variables: Mapping[str, str | None],
    *,
    status: HTTPStatus,
    headers: Mapping[str, str] | None = None,
    content_security_policy: str | None = None,
) -> BridgeResponse:
    response_headers = {
        "Content-Type": "text/html; charset=UTF-8",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }
    if content_security_policy is not None:
        response_headers["Content-Security-Policy"] = content_security_policy
    if headers is not None:
        response_headers.update(headers)

    return BridgeResponse(
        status=status,
        headers=response_headers,
        body=environment.from_string(template)
        .render(variables=variables, **variables)
        .encode("utf-8"),
    )
