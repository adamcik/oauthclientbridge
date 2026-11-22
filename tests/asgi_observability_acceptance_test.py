"""Durable observability contracts exercised through the ASGI HTTP boundary."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Literal, NoReturn

import httpx
import pytest
import pytest_sentry_capture
import sentry_sdk
from opentelemetry import trace
from pydantic import SecretStr
from pytest_otel_capture import OTelMocker, assert_trace_header
from pytest_sentry_capture import SentryCapture
from starlette.applications import Starlette
from starlette.requests import Request

from oauthclientbridge import asgi, crypto, db, logs, telemetry, types
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import SentrySettings, Settings
from tests.prometheus import sample_value

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("instrumented")]


@pytest.fixture
def initialize_sentry(
    sentry_transport: pytest_sentry_capture.FakeTransport,
    sentry_isolation_scope: None,
) -> None:
    # The capture plugin owns cleanup after successful initialization.
    try:
        telemetry.init_sentry(
            SentrySettings(
                enabled=True,
                dsn=SecretStr("https://key@example.invalid/1"),
                sample_rate=1.0,
                traces_sample_rate=1.0,
            ),
            "starlette",
            sentry_transport,
        )
    except BaseException:
        sentry_sdk.get_client().close()
        raise


@pytest.fixture
def acceptance_app(
    settings: Settings,
    instrumented: None,
    sentry_capture: SentryCapture,
) -> Starlette:
    async def unexpected_fetch(
        uri: str,
        upstream_grant_type: types.UpstreamGrantType,
        auth: str | None = None,
        **data: str | None,
    ) -> NoReturn:
        raise AssertionError("acceptance scenario must not contact the provider")

    return asgi.create_app(settings, fetch=unexpected_fetch, initialize_runtime=False)


@pytest.fixture
async def acceptance_client(
    acceptance_app: Starlette,
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=acceptance_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://bridge.example.com"
    ) as client:
        yield client


async def _assert_oauth_error_metric(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    error: OAuthError,
    status: HTTPStatus,
    before: float,
) -> None:
    response = await client.get("/metrics")
    assert (
        sample_value(
            response.content,
            "oauth_server_error_total",
            {
                "endpoint": endpoint,
                "error": error.value,
                "status": f"http_{status.name.lower()}",
            },
        )
        == before + 1
    )


@dataclass(frozen=True)
class ExpectedOAuthFailureCase:
    id: str
    method: Literal["GET", "POST"]
    path: str
    data: dict[str, str] | None
    error: OAuthError
    endpoint: str
    allowed_scopes: set[str] | None = None


@pytest.mark.parametrize(
    "case",
    [
        ExpectedOAuthFailureCase(
            id="authorize-wrong-redirect-uri",
            method="GET",
            path="/?redirect_uri=https://wrong.example.com/callback",
            data=None,
            error=OAuthError.INVALID_REQUEST,
            endpoint="authorize",
        ),
        ExpectedOAuthFailureCase(
            id="authorize-disallowed-scope",
            method="GET",
            path="/?scope=not-allowed",
            data=None,
            error=OAuthError.INVALID_SCOPE,
            endpoint="authorize",
            allowed_scopes={"allowed"},
        ),
        ExpectedOAuthFailureCase(
            id="callback-invalid-state",
            method="GET",
            path="/callback?code=1234",
            data=None,
            error=OAuthError.INVALID_STATE,
            endpoint="callback",
        ),
        ExpectedOAuthFailureCase(
            id="token-unsupported-grant",
            method="POST",
            path="/token",
            data={"grant_type": "authorization_code"},
            error=OAuthError.UNSUPPORTED_GRANT_TYPE,
            endpoint="token",
        ),
    ],
    ids=lambda case: case.id,
)
async def test_expected_oauth_failures_have_safe_observability(
    acceptance_client: httpx.AsyncClient,
    otel_mock: OTelMocker,
    sentry_capture: SentryCapture,
    case: ExpectedOAuthFailureCase,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = sample_value(
        telemetry.export_metrics(settings.prometheus),
        "oauth_server_error_total",
        {
            "endpoint": case.endpoint,
            "error": case.error.value,
            "status": "http_bad_request",
        },
    )
    logs.init_logging(settings.log)
    if case.allowed_scopes is not None:
        settings.oauth = settings.oauth.model_copy(
            update={"allowed_scopes": case.allowed_scopes}
        )
    response = await acceptance_client.request(case.method, case.path, data=case.data)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert json.loads(response.text)["error"] == case.error
    await _assert_oauth_error_metric(
        acceptance_client,
        endpoint=case.endpoint,
        error=case.error,
        status=HTTPStatus.BAD_REQUEST,
        before=before,
    )
    outcome_spans = [
        span
        for span in otel_mock.get_finished_spans()
        if span.attributes is not None
        and span.attributes.get("oauth.error") == case.error.value
    ]
    assert len(outcome_spans) == 1
    assert outcome_spans[0].status.status_code == trace.StatusCode.UNSET
    sentry_capture.flush()
    assert list(sentry_capture.get_exceptions()) == []
    access = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if '"logger": "oauthclientbridge.http"' in line
    ]
    assert len(access) == 2
    assert access[0]["oauth_error"] == case.error.value
    assert "oauth_error" not in access[1]


async def test_unexpected_token_failure_has_safe_response_and_error_observability(
    acceptance_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    otel_mock: OTelMocker,
    sentry_capture: SentryCapture,
    settings: Settings,
) -> None:
    before = sample_value(
        telemetry.export_metrics(settings.prometheus),
        "oauth_server_error_total",
        {
            "endpoint": "token",
            "error": OAuthError.SERVER_ERROR.value,
            "status": "http_internal_server_error",
        },
    )

    def fail(*_args: object) -> NoReturn:
        secret = crypto.generate_key()
        _ = secret
        raise RuntimeError("boom")

    # Substitute the storage boundary, not endpoint outcome handling.
    monkeypatch.setattr(db, "lookup", fail)
    response = await acceptance_client.post(
        "/token",
        data={
            "client_id": str(db.generate_id()),
            "client_secret": crypto.generate_key(),
            "grant_type": "client_credentials",
        },
    )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.json() == OAuthError.SERVER_ERROR.json()
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    await _assert_oauth_error_metric(
        acceptance_client,
        endpoint="token",
        error=OAuthError.SERVER_ERROR,
        status=HTTPStatus.INTERNAL_SERVER_ERROR,
        before=before,
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
    sentry_capture.flush()
    assert [exception["type"] for exception in sentry_capture.get_exceptions()] == [
        "RuntimeError"
    ]
    exception = next(sentry_capture.get_exceptions())
    assert exception["stacktrace"]["frames"]
    assert all("vars" not in frame for frame in exception["stacktrace"]["frames"])


async def test_unexpected_authorize_failure_has_safe_browser_response(
    acceptance_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    sentry_capture: SentryCapture,
) -> None:
    def fail() -> NoReturn:
        raise RuntimeError("internal detail")

    monkeypatch.setattr(crypto, "generate_key", fail)
    response = await acceptance_client.get("/")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert OAuthError.SERVER_ERROR.value in response.text
    assert "internal detail" not in response.text
    assert response.headers["Content-Type"] == "text/html; charset=UTF-8"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    sentry_capture.flush()
    assert [exception["type"] for exception in sentry_capture.get_exceptions()] == [
        "RuntimeError"
    ]


@pytest.mark.parametrize("endpoint", ["metrics", "unknown"])
async def test_non_oauth_failure_is_safe_and_not_an_oauth_outcome(
    acceptance_app: Starlette,
    acceptance_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    otel_mock: OTelMocker,
    sentry_capture: SentryCapture,
    endpoint: str,
) -> None:
    def fail(*_: object) -> NoReturn:
        raise RuntimeError("internal detail")

    async def crash(_: Request) -> NoReturn:
        fail()

    if endpoint == "metrics":
        monkeypatch.setattr(telemetry, "export_metrics", fail)
        path = "/metrics"
    else:
        acceptance_app.add_route("/unexpected", crash)
        path = "/unexpected"
    response = await acceptance_client.get(path)

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.text == "Internal Server Error"
    assert response.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    sentry_capture.flush()
    assert [exception["type"] for exception in sentry_capture.get_exceptions()] == [
        "RuntimeError"
    ]
    spans = otel_mock.get_finished_spans()
    assert all(
        span.attributes is None or "oauth.error" not in span.attributes
        for span in spans
    )
    server_spans = [
        span for span in spans if span.readable_span.kind == trace.SpanKind.SERVER
    ]
    assert len(server_spans) == 1
    assert server_spans[0].status.status_code == trace.StatusCode.ERROR
    assert any(event.name == "exception" for event in server_spans[0].events)
    assert server_spans[0].attributes is not None
    assert server_spans[0].attributes["error.unhandled"] is True
    assert server_spans[0].attributes["oauth.endpoint"] == endpoint


async def test_framework_native_routing_responses_remain_unhandled(
    acceptance_client: httpx.AsyncClient,
    sentry_capture: SentryCapture,
) -> None:
    assert (
        await acceptance_client.get("/not-found")
    ).status_code == HTTPStatus.NOT_FOUND
    assert (
        await acceptance_client.get("/token")
    ).status_code == HTTPStatus.METHOD_NOT_ALLOWED
    sentry_capture.flush()
    assert list(sentry_capture.get_exceptions()) == []


async def test_callback_logs_and_traces_redact_all_query_values(
    acceptance_client: httpx.AsyncClient,
    settings: Settings,
    otel_mock: OTelMocker,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logs.init_logging(settings.log)
    response = await acceptance_client.get(
        "/callback?code=private-code&state=private-state&next=%2Fprivate-home",
        headers={"Referer": "https://example.com/private-referrer"},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    access = [r for r in records if r.get("logger") == "oauthclientbridge.http"]
    assert len(access) == 1
    assert access[0]["url.query"] == (
        "code=<REDACTED>&state=<REDACTED>&next=<REDACTED>"
    )
    server_span = next(
        span
        for span in otel_mock.get_finished_spans()
        if span.readable_span.kind == trace.SpanKind.SERVER
    )
    assert server_span.attributes is not None
    assert server_span.attributes["url.query"] == access[0]["url.query"]
    exported = json.dumps(access) + json.dumps(dict(server_span.attributes))
    for secret in ("private-code", "private-state", "private-home", "private-referrer"):
        assert secret not in exported


async def test_authorize_correlates_trace_and_redacts_redirect_location(
    acceptance_client: httpx.AsyncClient,
    otel_mock: OTelMocker,
) -> None:
    trace_id = 0x1234567890ABCDEF
    response = await acceptance_client.get(
        "/", headers={"traceparent": f"00-{trace_id:032x}-0000000000123456-01"}
    )

    assert response.status_code == HTTPStatus.FOUND
    server_span = next(
        span
        for span in otel_mock.get_finished_spans()
        if span.readable_span.kind == trace.SpanKind.SERVER
    )
    assert_trace_header(response.headers["traceresponse"], server_span.trace_id)
    assert server_span.trace_id == trace_id
    assert server_span.attributes is not None
    location = server_span.attributes["http.response.header.location"]
    assert location.startswith("https://provider.example.com/auth?")
    assert "state=<REDACTED>" in location
    assert "client_secret" not in location


@dataclass(frozen=True)
class IncomingTraceCase:
    id: str
    headers: dict[str, str]
    trace_id: int


@pytest.mark.parametrize(
    "case",
    [
        IncomingTraceCase(
            id="w3c-only",
            headers={
                "traceparent": "00-00000000000000001234567890abcdef-0000000000123456-01"
            },
            trace_id=0x1234567890ABCDEF,
        ),
        IncomingTraceCase(
            id="sentry-only",
            headers={
                "sentry-trace": "0000000000000000fedcba0987654321-0000000000654321-1"
            },
            trace_id=0xFEDCBA0987654321,
        ),
        IncomingTraceCase(
            id="w3c-takes-precedence",
            headers={
                "traceparent": "00-00000000000000001234567890abcdef-0000000000123456-01",
                "sentry-trace": "0000000000000000fedcba0987654321-0000000000654321-1",
            },
            trace_id=0x1234567890ABCDEF,
        ),
        IncomingTraceCase(
            id="invalid-w3c-retains-sentry",
            headers={
                "traceparent": "invalid",
                "sentry-trace": "0000000000000000fedcba0987654321-0000000000654321-1",
            },
            trace_id=0xFEDCBA0987654321,
        ),
    ],
    ids=lambda case: case.id,
)
async def test_sentry_enabled_incoming_trace_propagation(
    acceptance_app: Starlette,
    otel_mock: OTelMocker,
    case: IncomingTraceCase,
) -> None:
    # Enter at the transport boundary so client-side SDK instrumentation cannot
    # add or replace the incoming headers under test.
    async with httpx.ASGITransport(app=acceptance_app) as transport:
        response = await transport.handle_async_request(
            httpx.Request("GET", "https://bridge.example.com/", headers=case.headers)
        )
        await response.aread()
        await response.aclose()

    server_spans = [
        span
        for span in otel_mock.get_finished_spans()
        if span.readable_span.kind == trace.SpanKind.SERVER
    ]
    assert len(server_spans) == 1
    assert server_spans[0].trace_id == case.trace_id
    assert_trace_header(response.headers["traceresponse"], case.trace_id)
