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


def render_browser_oauth_result(
    template: str,
    *,
    status: HTTPStatus,
    content_security_policy: str | None,
    client_id: str | None = None,
    client_secret: str | None = None,
    state: str | None = None,
    error: str | None = None,
    description: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> BridgeResponse:
    return render_template(
        template,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "state": state,
            "error": error,
            "description": description,
        },
        status=status,
        headers=headers,
        content_security_policy=content_security_policy,
    )
