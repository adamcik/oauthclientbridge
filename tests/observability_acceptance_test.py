import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import Literal, NoReturn

import pytest
from flask import Flask
from flask.testing import FlaskClient
from opentelemetry import trace
from prometheus_client.parser import text_string_to_metric_families

from oauthclientbridge import crypto, db, telemetry
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import Settings
from pytest_otel_capture import OTelMocker
from pytest_sentry_capture import SentryCapture

from .conftest import TokenTuple

# Instrument Flask before constructing the app used by each acceptance scenario.
pytestmark = pytest.mark.usefixtures("instrumented")


def _assert_oauth_error_metric(
    client: FlaskClient,
    *,
    endpoint: str,
    error: OAuthError,
    status: HTTPStatus,
) -> None:
    expected_labels = {
        "endpoint": endpoint,
        "error": error.value,
        "status": f"http_{status.name.lower()}",
    }
    metrics = text_string_to_metric_families(client.get("/metrics").text)
    server_errors = next(
        metric for metric in metrics if metric.name == "oauth_server_error"
    )
    assert any(
        sample.name == "oauth_server_error_total" and sample.labels == expected_labels
        for sample in server_errors.samples
    )


@dataclass(frozen=True)
class ExpectedOAuthFailure:
    name: str
    method: Literal["GET", "POST"]
    path: str
    data: dict[str, str] | None
    error: OAuthError
    endpoint: str
    allowed_scopes: set[str] | None = None


@pytest.mark.parametrize(
    "case",
    [
        ExpectedOAuthFailure(
            name="authorize wrong redirect URI",
            method="GET",
            path="/?redirect_uri=https://wrong.example.com/callback",
            data=None,
            error=OAuthError.INVALID_REQUEST,
            endpoint="authorize",
        ),
        ExpectedOAuthFailure(
            name="authorize disallowed scope",
            method="GET",
            path="/?scope=not-allowed",
            data=None,
            error=OAuthError.INVALID_SCOPE,
            endpoint="authorize",
            allowed_scopes={"allowed"},
        ),
        ExpectedOAuthFailure(
            name="callback invalid state",
            method="GET",
            path="/callback?code=1234",
            data=None,
            error=OAuthError.INVALID_STATE,
            endpoint="callback",
        ),
        ExpectedOAuthFailure(
            name="token unsupported grant",
            method="POST",
            path="/token",
            data={"grant_type": "authorization_code"},
            error=OAuthError.UNSUPPORTED_GRANT_TYPE,
            endpoint="token",
        ),
    ],
    ids=lambda case: case.name,
)
def test_expected_oauth_failures_have_safe_observability(
    client: FlaskClient,
    otel_mock: OTelMocker,
    sentry_capture: SentryCapture,
    case: ExpectedOAuthFailure,
    settings: Settings,
) -> None:
    if case.allowed_scopes is not None:
        settings.oauth = settings.oauth.model_copy(
            update={"allowed_scopes": case.allowed_scopes}
        )
    response = client.open(case.path, method=case.method, data=case.data)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert json.loads(response.text)["error"] == case.error
    _assert_oauth_error_metric(
        client,
        endpoint=case.endpoint,
        error=case.error,
        status=HTTPStatus.BAD_REQUEST,
    )

    outcome_spans = [
        span
        for span in otel_mock.get_finished_spans()
        if span.attributes is not None
        and span.attributes.get("oauth.error") == case.error.value
    ]
    assert len(outcome_spans) == 1
    assert outcome_spans[0].status.status_code == trace.StatusCode.UNSET
    assert list(sentry_capture.get_exceptions()) == []


def test_unexpected_token_failure_has_safe_response_and_error_observability(
    client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
    otel_mock: OTelMocker,
    sentry_capture: SentryCapture,
    access_token: TokenTuple,
) -> None:
    def fail(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "lookup", fail)

    response = client.post(
        "/token",
        data={
            "client_id": access_token.client_id,
            "client_secret": access_token.client_secret,
            "grant_type": "client_credentials",
        },
    )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.json == OAuthError.SERVER_ERROR.json()
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    _assert_oauth_error_metric(
        client,
        endpoint="token",
        error=OAuthError.SERVER_ERROR,
        status=HTTPStatus.INTERNAL_SERVER_ERROR,
    )

    outcome_spans = [
        span
        for span in otel_mock.get_finished_spans()
        if span.attributes is not None
        and span.attributes.get("oauth.error") == OAuthError.SERVER_ERROR.value
    ]
    assert len(outcome_spans) == 1
    assert outcome_spans[0].status.status_code == trace.StatusCode.ERROR
    assert any(
        event.name == "exception"
        and event.attributes is not None
        and event.attributes["exception.type"] == "RuntimeError"
        for event in outcome_spans[0].events
    )
    exceptions = list(sentry_capture.get_exceptions())
    assert [exception["type"] for exception in exceptions] == ["RuntimeError"]


def test_unexpected_authorize_failure_has_safe_browser_response(
    client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
    sentry_capture: SentryCapture,
) -> None:
    def fail() -> str:
        raise RuntimeError("internal detail")

    monkeypatch.setattr(crypto, "generate_key", fail)

    response = client.get("/")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert OAuthError.SERVER_ERROR.value in response.text
    assert "internal detail" not in response.text
    assert response.headers["Content-Type"] == "text/html; charset=UTF-8"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert [exception["type"] for exception in sentry_capture.get_exceptions()] == [
        "RuntimeError"
    ]


def test_metrics_failure_is_safe_and_not_an_oauth_outcome(
    client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
    otel_mock: OTelMocker,
    sentry_capture: SentryCapture,
) -> None:
    def fail() -> NoReturn:
        raise RuntimeError("internal detail")

    monkeypatch.setattr(telemetry, "export_metrics", fail)

    response = client.get("/metrics")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.text == "Internal Server Error"
    assert response.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert "internal detail" not in response.text
    assert [exception["type"] for exception in sentry_capture.get_exceptions()] == [
        "RuntimeError"
    ]
    assert all(
        span.attributes is None or "oauth.error" not in span.attributes
        for span in otel_mock.get_finished_spans()
    )


def test_framework_native_routing_responses_remain_unhandled(
    client: FlaskClient,
    sentry_capture: SentryCapture,
) -> None:
    assert client.get("/not-found").status_code == HTTPStatus.NOT_FOUND
    assert client.get("/token").status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert list(sentry_capture.get_exceptions()) == []


def test_unknown_failure_is_safe_and_not_an_oauth_outcome(
    app: Flask,
    client: FlaskClient,
    otel_mock: OTelMocker,
    sentry_capture: SentryCapture,
) -> None:
    def fail() -> None:
        raise RuntimeError("internal detail")

    app.add_url_rule("/unexpected", "unexpected", fail)

    response = client.get("/unexpected")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.text == "Internal Server Error"
    assert response.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert "internal detail" not in response.text
    assert [exception["type"] for exception in sentry_capture.get_exceptions()] == [
        "RuntimeError"
    ]
    assert all(
        span.attributes is None or "oauth.error" not in span.attributes
        for span in otel_mock.get_finished_spans()
    )
