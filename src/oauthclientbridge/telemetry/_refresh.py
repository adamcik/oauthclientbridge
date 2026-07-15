"""Background refresh lifecycle for asynchronously collected metrics."""

import logging
from collections.abc import Callable
from random import uniform

import flask
from flask import Flask
from opentelemetry import trace

from oauthclientbridge.utils import coalescing

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


def add_refresher(app: Flask, refresher: Callable[[], None]) -> None:
    """Register a callback that updates metrics derived from application state."""
    refreshers = app.extensions.setdefault("oauth_metrics_refreshers", [])
    refreshers.append(refresher)


def refresh_once(app: Flask | None = None) -> None:
    """Run every registered refresh callback in the selected application."""
    current = app or flask.current_app
    refreshers = current.extensions.get("oauth_metrics_refreshers", [])
    for refresher in refreshers:
        refresher()


def request_refresh(app: Flask | None = None) -> None:
    """Coalesce a refresh request when the background worker is running."""
    current = app or flask.current_app
    worker = current.extensions.get("oauth_metrics_refresh_worker")
    if worker is not None:
        worker.request()


def start_background_refresh(app: Flask) -> None:
    """Start the refresh worker when this application has refresh callbacks."""
    if app.extensions.get("oauth_metrics_refresh_worker") is not None:
        return

    if not app.extensions.get("oauth_metrics_refreshers", []):
        return

    worker = coalescing.CoalescingWorker(
        lambda: _refresh_metrics_in_app(app),
        debounce_seconds=0.5,
        startup_delay=lambda: uniform(0, 5.0),
        name="oauth-metrics-refresh",
    )
    app.extensions["oauth_metrics_refresh_worker"] = worker
    worker.start()
    worker.request()


def stop_background_refresh(app: Flask) -> None:
    """Stop and remove the application's metrics refresh worker."""
    worker = app.extensions.pop("oauth_metrics_refresh_worker", None)
    if worker is not None:
        worker.stop(timeout=1.0)


def _refresh_metrics_in_app(app: Flask) -> None:
    try:
        with app.app_context():
            with tracer.start_as_current_span("METRICS refresh"):
                refresh_once(app)
    except Exception:
        logger.exception("Metrics refresh failed")
