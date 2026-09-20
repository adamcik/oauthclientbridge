from dataclasses import dataclass, field
from http import HTTPStatus

import pytest

from oauthclientbridge import bridge, endpoint_execution, types
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import Settings


@dataclass
class RecordingFallbackObserver:
    failures: list[tuple[types.Endpoint, BaseException]] = field(default_factory=list)

    def observe(self, endpoint: types.Endpoint, exception: BaseException) -> None:
        self.failures.append((endpoint, exception))


@dataclass
class RecordingOAuthOutcomeObserver:
    outcomes: list[tuple[types.Endpoint, HTTPStatus, OAuthError | None]] = field(
        default_factory=list
    )

    def observe(
        self, endpoint: types.Endpoint, status: HTTPStatus, error: OAuthError | None
    ) -> None:
        self.outcomes.append((endpoint, status, error))


class FailingFallbackObserver:
    def observe(self, endpoint: types.Endpoint, exception: BaseException) -> None:
        raise RuntimeError("fallback observer unavailable")


class FailingOutcomeObserver:
    def observe(
        self, endpoint: types.Endpoint, status: HTTPStatus, error: OAuthError | None
    ) -> None:
        raise RuntimeError("outcome observer unavailable")


@dataclass(frozen=True)
class FallbackCase:
    name: str
    endpoint: types.Endpoint
    expected_body: bytes | dict[str, str]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        FallbackCase(
            name="authorization browser result",
            endpoint=types.Endpoint.AUTHORIZE,
            expected_body=b'"error": "server_error"',
        ),
        FallbackCase(
            name="callback browser result",
            endpoint=types.Endpoint.CALLBACK,
            expected_body=b'"error": "server_error"',
        ),
        FallbackCase(
            name="token OAuth JSON",
            endpoint=types.Endpoint.TOKEN,
            expected_body=OAuthError.SERVER_ERROR.json(),
        ),
    ],
    ids=lambda case: case.name,
)
async def test_execution_converts_oauth_faults_to_safe_responses(
    settings: Settings, case: FallbackCase
) -> None:
    fallback_observer = RecordingFallbackObserver()
    outcome_observer = RecordingOAuthOutcomeObserver()
    failure = RuntimeError("internal detail")

    async def fail() -> bridge.BridgeResult:
        raise failure

    result = await endpoint_execution.run(
        settings,
        fallback_observer,
        outcome_observer,
        case.endpoint,
        fail,
    )
    response = result.response

    assert response.status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    if isinstance(case.expected_body, bytes):
        assert isinstance(response.body, bytes)
        assert case.expected_body in response.body
    else:
        assert response.body == case.expected_body
    assert fallback_observer.failures == [(case.endpoint, failure)]
    assert outcome_observer.outcomes == [
        (case.endpoint, HTTPStatus.INTERNAL_SERVER_ERROR, OAuthError.SERVER_ERROR)
    ]
    assert result.log_context == {
        "oauth_endpoint": case.endpoint.value,
        "oauth_error": OAuthError.SERVER_ERROR.value,
    }


@pytest.mark.anyio
async def test_execution_ignores_observer_failures(settings: Settings) -> None:
    async def succeed() -> bridge.BridgeResult:
        return bridge.BridgeResult(
            bridge.BridgeResponse(status=HTTPStatus.FOUND), oauth_error=None
        )

    result = await endpoint_execution.run(
        settings,
        FailingFallbackObserver(),
        FailingOutcomeObserver(),
        types.Endpoint.AUTHORIZE,
        succeed,
    )

    assert result.response.status == HTTPStatus.FOUND
    assert result.log_context == {"oauth_endpoint": "authorize"}

    async def fail() -> bridge.BridgeResult:
        raise RuntimeError("endpoint failed")

    fallback_result = await endpoint_execution.run(
        settings,
        FailingFallbackObserver(),
        FailingOutcomeObserver(),
        types.Endpoint.TOKEN,
        fail,
    )

    assert fallback_result.response.status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert fallback_result.log_context == {
        "oauth_endpoint": "token",
        "oauth_error": OAuthError.SERVER_ERROR.value,
    }
