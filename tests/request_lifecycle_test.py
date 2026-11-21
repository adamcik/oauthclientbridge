"""ASGI request lifecycle observability contracts."""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from http import HTTPStatus

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families
from pytest_otel_capture import OTelMocker

from oauthclientbridge import logs
from oauthclientbridge.asgi import create_app as create_asgi_app
from oauthclientbridge.settings import Settings


@asynccontextmanager
async def _client(settings: Settings) -> AsyncGenerator[httpx.AsyncClient]:
    app = create_asgi_app(settings, initialize_runtime=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://bridge.example.com",
        follow_redirects=False,
    ) as client:
        yield client


@pytest.mark.anyio
@pytest.mark.usefixtures("instrumented")
async def test_request_lifecycle_sanitizes_and_clears_context(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    otel_mock: OTelMocker,
) -> None:
    logs.init_logging(settings.log)

    async with _client(settings) as client:
        first = await client.get("/?state=secret-state")
        second = await client.get("/not-found")

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

    server_span = next(
        span
        for span in otel_mock.get_finished_spans()
        if span.name.startswith("GET")
        and span.attributes is not None
        and "url.full" in span.attributes
    )
    assert server_span.attributes is not None
    assert server_span.attributes["url.query"] == "state=<REDACTED>"
    assert "secret-state" not in server_span.attributes["url.full"]
    location = server_span.attributes["http.response.header.location"]
    assert isinstance(location, str)
    assert location.startswith("https://provider.example.com/auth?")


@pytest.mark.anyio
async def test_request_lifecycle_metrics_use_stable_endpoints(
    settings: Settings,
) -> None:
    async with _client(settings) as client:
        _ = await client.get("/")
        not_found = await client.get("/not-found")
        method_not_allowed = await client.get("/token")
        metrics = await client.get("/metrics")

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
