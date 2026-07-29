"""Request lifecycle observability shared by the Flask and Starlette adapters."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Literal

import anyio
import httpx
import pytest
from flask.testing import FlaskClient
from prometheus_client.parser import text_string_to_metric_families

from oauthclientbridge import create_app, logs
from oauthclientbridge.asgi import create_app as create_asgi_app
from oauthclientbridge.settings import Settings
from pytest_otel_capture import OTelMocker


@asynccontextmanager
async def _client(
    adapter: Literal["flask", "starlette"], settings: Settings
) -> AsyncIterator[httpx.AsyncClient | FlaskClient]:
    if adapter == "flask":
        app = create_app(settings)
        app.secret_key = "test-secret-key"
        yield app.test_client()
        return

    app = create_asgi_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://bridge.example.com",
        follow_redirects=False,
    ) as client:
        yield client


async def _get(client: httpx.AsyncClient | FlaskClient, path: str):
    if isinstance(client, FlaskClient):
        return await anyio.to_thread.run_sync(lambda: client.get(path))
    return await client.get(path)


@pytest.mark.anyio
@pytest.mark.usefixtures("instrumented")
@pytest.mark.parametrize("adapter", ["flask", "starlette"])
async def test_request_lifecycle_sanitizes_and_clears_context(
    adapter: Literal["flask", "starlette"],
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    otel_mock: OTelMocker,
) -> None:
    logs.init_logging(settings.log)

    async with _client(adapter, settings) as client:
        first = await _get(client, "/?state=secret-state")
        second = await _get(client, "/not-found")

    assert first.status_code == HTTPStatus.FOUND
    assert second.status_code == HTTPStatus.NOT_FOUND

    records = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if '"logger": "oauthclientbridge.http"' in line
    ]
    assert len(records) == 2
    assert records[0]["url.query"] == "state=<REDACTED>"
    assert "secret-state" not in records[0]["url.full"]
    assert records[0]["http.response.status_code"] == HTTPStatus.FOUND
    assert records[0]["http.response.body.size"] is not None
    assert "trace_id" in records[0]
    assert "oauth_endpoint" not in records[1]

    if adapter == "starlette":
        assert any(
            span.name.startswith("GET") for span in otel_mock.get_finished_spans()
        )


@pytest.mark.anyio
@pytest.mark.parametrize("adapter", ["flask", "starlette"])
async def test_request_lifecycle_metrics_use_stable_endpoints(
    adapter: Literal["flask", "starlette"], settings: Settings
) -> None:
    async with _client(adapter, settings) as client:
        _ = await _get(client, "/")
        not_found = await _get(client, "/not-found")
        method_not_allowed = await _get(client, "/token")
        metrics = await _get(client, "/metrics")

    assert metrics.status_code == HTTPStatus.OK
    assert not_found.status_code == HTTPStatus.NOT_FOUND
    assert method_not_allowed.status_code == HTTPStatus.METHOD_NOT_ALLOWED

    families = text_string_to_metric_families(metrics.text)
    lifecycle = next(
        family for family in families if family.name == "oauth_server_latency_seconds"
    )
    labels = [
        sample.labels for sample in lifecycle.samples if sample.name.endswith("_count")
    ]
    assert {"endpoint": "authorize", "status": "http_found"} in labels
    assert {"endpoint": "unknown", "status": "http_not_found"} in labels
    assert {"endpoint": "unknown", "status": "http_method_not_allowed"} in labels
