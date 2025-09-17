"""
A pytest plugin for testing Sentry integration.

This plugin provides a `sentry_pytest` fixture that captures and allows you to
inspect Sentry events, transactions, and other data sent during your tests.
"""

from typing import Any, Generator, override

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event


class FakeTransport(Transport):
    def __init__(self):
        super().__init__()
        self.captured_envelopes: list[Envelope] = []

    @override
    def capture_envelope(self, envelope: Envelope):
        self.captured_envelopes.append(envelope)


class SentryCapture:
    """A helper class for inspecting Sentry data captured during tests."""

    def __init__(self, envelopes: list[Envelope]):
        self.envelopes: list[Envelope] = envelopes

    def get_events(self) -> Generator[Event, None, None]:
        """Returns all captured event payloads."""
        sentry_sdk.flush()
        for envelope in self.envelopes:
            event = envelope.get_event()
            if event is not None:
                yield event

    def get_transactions(self) -> Generator[Event, None, None]:
        """Returns all captured transaction payloads."""
        sentry_sdk.flush()
        for envelope in self.envelopes:
            event = envelope.get_transaction_event()
            if event is not None:
                yield event

    def get_spans(self) -> Generator[dict[str, Any], None, None]:
        for t in self.get_transactions():
            for span in t.get("spans", []):
                yield span

    def get_breadcrumbs(self) -> Generator[dict[str, Any], None, None]:
        for e in self.get_events():
            for crumb in e.get("breadcrumbs", {}).get("values", []):
                yield crumb

    def get_exceptions(self) -> Generator[dict[str, Any], None, None]:
        for e in self.get_events():
            for exc in e.get("exception", {}).get("values", []):
                yield exc

    def find_transaction_by_name(self, name: str) -> Event:
        """Finds transaction by its operation name."""
        for t in self.get_transactions():
            if t.get("transaction") == name:
                return t
        raise LookupError(f"Transaction with transaction='{name}' not found.")

    def find_span_by_op(self, op: str) -> dict[str, object]:
        """Finds a span in a transaction by its operation name."""
        for span in self.get_spans():
            if span.get("op") == op:
                return span
        raise LookupError(f"Span with op='{op}' not found.")

    def find_breadcrumb_by_message(self, message: str) -> dict[str, Any]:
        """Finds a breadcrumb in an event by its message."""
        for crumb in self.get_breadcrumbs():
            if crumb.get("message") == message:
                return crumb
        raise LookupError(f"Breadcrumb with message='{message}' not found.")

    def find_breadcrumb_by_level(self, level: str) -> dict[str, Any]:
        """Finds a breadcrumb in an event by its level."""
        for crumb in self.get_breadcrumbs():
            if crumb.get("level") == level:
                return crumb
        raise LookupError(f"Breadcrumb with level='{level}' not found.")

    def find_exception_by_type(self, exception_type: str) -> dict[str, Any]:
        """Finds an exception in an event by its type."""
        for e in self.get_exceptions():
            if e.get("type") == exception_type:
                return e
        raise LookupError(f"Exception with type='{exception_type}' not found.")


@pytest.fixture(autouse=True)
def sentry_isolation_scope():
    """Always run with an isolated blank slate."""
    with sentry_sdk.isolation_scope():
        _ = sentry_sdk.init()
        yield
    sentry_sdk.flush()
    sentry_sdk.get_client().close()


@pytest.fixture
def sentry_transport():
    return FakeTransport()


@pytest.fixture
def sentry_capture(sentry_transport, sentry_isolation_scope):
    _ = sentry_sdk.init(
        transport=sentry_transport,
        traces_sample_rate=1.0,
    )
    yield SentryCapture(sentry_transport.captured_envelopes)
