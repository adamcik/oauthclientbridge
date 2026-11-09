"""Compatibility imports for Sentry capture types, not plugin registration."""

from pytest_sentry_capture import (
    FakeTransport,
    SentryCapture,
)

__all__ = [
    "FakeTransport",
    "SentryCapture",
]
