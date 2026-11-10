from http import HTTPStatus

import jinja2.sandbox
import pytest

from oauthclientbridge.bridge import _template


def test_render_template_includes_variables_and_security_headers():
    response = _template.render_template(
        "{{ error }}: {{ description }}",
        {"error": "invalid_request", "description": "Invalid request."},
        status=HTTPStatus.BAD_REQUEST,
        content_security_policy="default-src 'none'",
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
        "{{ value }}", {"value": "<script>"}, status=HTTPStatus.OK
    )

    assert response.body == b"&lt;script&gt;"


def test_render_template_omits_disabled_content_security_policy():
    response = _template.render_template("callback", {}, status=HTTPStatus.BAD_REQUEST)

    assert "Content-Security-Policy" not in response.headers


def test_html_template_sandbox_rejects_private_attribute_traversal():
    with pytest.raises(jinja2.sandbox.SecurityError):
        _template.render_template(
            "{{ value.__class__.__mro__ }}", {"value": "text"}, status=HTTPStatus.OK
        )


def test_html_template_preserves_optional_variables_and_filters():
    response = _template.render_template(
        "{% if missing %}optional{% endif %}{{ value|upper }}",
        {"value": "<text>"},
        status=HTTPStatus.OK,
    )

    assert response.body == b"&lt;TEXT&gt;"
