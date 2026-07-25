import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import Literal

import pytest
from flask.testing import FlaskClient
from opentelemetry import trace
from prometheus_client.parser import text_string_to_metric_families

from oauthclientbridge import db
from oauthclientbridge.errors import OAuthError
from pytest_otel_capture import OTelMocker
from pytest_sentry_capture import SentryCapture

from .conftest import TokenTuple


@pytest.fixture
def traced_client(instrumented: None, client: FlaskClient) -> FlaskClient:
    _ = instrumented
    return client


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


@pytest.mark.parametrize(
    "case",
    [
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
    traced_client: FlaskClient,
    otel_mock: OTelMocker,
    sentry_capture: SentryCapture,
    case: ExpectedOAuthFailure,
) -> None:
    response = traced_client.open(case.path, method=case.method, data=case.data)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert json.loads(response.text)["error"] == case.error
    _assert_oauth_error_metric(
        traced_client,
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
    traced_client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
    otel_mock: OTelMocker,
    sentry_capture: SentryCapture,
    access_token: TokenTuple,
) -> None:
    def fail(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "lookup", fail)

    response = traced_client.post(
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
        traced_client,
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
    exceptions = list(sentry_capture.get_exceptions())
    assert [exception["type"] for exception in exceptions] == ["RuntimeError"]
