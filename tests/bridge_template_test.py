import base64
import re
from http import HTTPStatus
from pathlib import Path

import jinja2
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


def test_render_template_shares_one_nonce_without_exposing_it_as_oauth_data():
    response = _template.render_template(
        '<script nonce="{{ csp_nonce }}"></script>'
        '<style nonce="{{ csp_nonce }}"></style>{{ variables|tojson }}',
        {"error": "invalid_state"},
        status=HTTPStatus.BAD_REQUEST,
        content_security_policy=(
            "script-src 'nonce-{{ csp_nonce }}'; style-src 'nonce-{{ csp_nonce }}'"
        ),
    )

    assert isinstance(response.body, bytes)
    match = re.search(rb'nonce="([A-Za-z0-9_-]+)"', response.body)
    assert match is not None
    nonce = match[1].decode("ascii")
    assert len(base64.urlsafe_b64decode(nonce + "=" * (-len(nonce) % 4))) >= 16
    assert (
        response.body
        == (
            f'<script nonce="{nonce}"></script><style nonce="{nonce}"></style>'
            '{"error": "invalid_state"}'
        ).encode()
    )
    assert response.headers["Content-Security-Policy"] == (
        f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'"
    )


def test_render_template_nonce_is_fresh_per_response():
    first = _template.render_template("{{ csp_nonce }}", {}, status=HTTPStatus.OK)
    second = _template.render_template("{{ csp_nonce }}", {}, status=HTTPStatus.OK)

    assert first.body
    assert second.body
    assert first.body != second.body


def test_render_template_reserves_nonce_and_accepts_jinja_whitespace():
    response = _template.render_template(
        "{{ csp_nonce }}",
        {"csp_nonce": "untrusted", "error": "<script>"},
        status=HTTPStatus.OK,
        content_security_policy="script-src 'nonce-{{csp_nonce}}'",
    )

    assert isinstance(response.body, bytes)
    nonce = response.body.decode("ascii")
    assert nonce != "untrusted"
    assert response.headers["Content-Security-Policy"] == f"script-src 'nonce-{nonce}'"


@pytest.mark.parametrize(
    "name", ["error", "client_secret", "variables", "range", "lipsum"]
)
def test_csp_template_rejects_unknown_variables_and_globals(name: str):
    with pytest.raises(jinja2.UndefinedError):
        _template.render_template(
            "callback",
            {"error": "untrusted", "client_secret": "secret"},
            status=HTTPStatus.OK,
            content_security_policy="{{ " + name + " }}",
        )


@pytest.mark.parametrize(
    "expression", ["{{ csp_nonce|upper }}", "{{ csp_nonce is string }}"]
)
def test_csp_template_has_no_filters_or_tests(expression: str):
    with pytest.raises(jinja2.TemplateAssertionError):
        _template.render_template(
            "callback", {}, status=HTTPStatus.OK, content_security_policy=expression
        )


def test_csp_template_supports_sandboxed_jinja_without_html_escaping():
    response = _template.render_template(
        "{{ csp_nonce }}",
        {},
        status=HTTPStatus.OK,
        content_security_policy=(
            "{% if csp_nonce %}script-src 'nonce-{{ csp_nonce }}'; {% endif %}"
            "report-uri https://example.com/csp?a=1&b={{ 1 + 1 }}"
        ),
    )

    assert isinstance(response.body, bytes)
    nonce = response.body.decode("ascii")
    assert response.headers["Content-Security-Policy"] == (
        f"script-src 'nonce-{nonce}'; report-uri https://example.com/csp?a=1&b=2"
    )


def test_csp_template_sandbox_rejects_private_attribute_access():
    with pytest.raises(jinja2.sandbox.SecurityError):
        _template.render_template(
            "callback",
            {},
            status=HTTPStatus.OK,
            content_security_policy="{{ csp_nonce.__class__ }}",
        )


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


@pytest.mark.parametrize("provider", ["spotify", "soundcloud"])
def test_bundled_callback_template_and_policies_authorize_script(provider: str):
    deploy = Path(__file__).resolve().parents[1] / "deploy" / provider
    template = (deploy / "callback.html").read_text()
    for env_file in ("env", "asgi.env"):
        policy = next(
            line.split("=", 1)[1]
            for line in (deploy / env_file).read_text().splitlines()
            if line.startswith("BRIDGE_CALLBACK_CONTENT_SECURITY_POLICY=")
        )
        response = _template.render_browser_oauth_result(
            template,
            status=HTTPStatus.OK,
            content_security_policy=policy,
            client_secret='</script><script>alert("injected")</script>',
        )

        assert isinstance(response.body, bytes)
        match = re.search(rb'<script nonce="([A-Za-z0-9_-]+)">', response.body)
        assert match is not None
        nonce = match[1].decode("ascii")
        assert (
            f"script-src 'nonce-{nonce}'" in response.headers["Content-Security-Policy"]
        )
        assert b"{{ csp_nonce }}" not in response.body
        assert response.body.count(b"<script") == 1
        assert response.body.count(b"</script>") == 1
        assert response.headers["Cache-Control"] == "no-store"
