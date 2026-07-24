from http import HTTPStatus

from oauthclientbridge.bridge import _template


def test_render_template_includes_variables_and_security_headers():
    response = _template.render_template(
        "{{ error }}: {{ description }}",
        {"error": "invalid_request", "description": "Invalid request."},
        "default-src 'none'",
        HTTPStatus.BAD_REQUEST,
    )

    assert response.status == HTTPStatus.BAD_REQUEST
    assert response.body == b"invalid_request: Invalid request."
    assert response.headers == {
        "Content-Type": "text/html; charset=UTF-8",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        "Content-Security-Policy": "default-src 'none'",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }


def test_render_template_autoescapes_variables():
    response = _template.render_template(
        "{{ value }}", {"value": "<script>"}, None, HTTPStatus.OK
    )

    assert response.body == b"&lt;script&gt;"


def test_render_template_omits_disabled_content_security_policy():
    response = _template.render_template("callback", {}, None, HTTPStatus.BAD_REQUEST)

    assert "Content-Security-Policy" not in response.headers
