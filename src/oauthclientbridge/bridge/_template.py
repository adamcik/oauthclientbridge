from collections.abc import Mapping
from http import HTTPStatus

import jinja2

from ._types import BridgeResponse

environment = jinja2.Environment(autoescape=True)


def render_template(
    template: str,
    variables: Mapping[str, str | None],
    content_security_policy: str | None,
    status: HTTPStatus,
) -> BridgeResponse:
    headers = {
        "Content-Type": "text/html; charset=UTF-8",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    }
    if content_security_policy is not None:
        headers["Content-Security-Policy"] = content_security_policy

    return BridgeResponse(
        status=status,
        headers=headers,
        body=environment.from_string(template)
        .render(variables=variables, **variables)
        .encode("utf-8"),
    )
