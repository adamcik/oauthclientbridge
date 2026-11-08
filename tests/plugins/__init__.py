from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Pytest must import plugins after installing assertion rewriting.
    from . import otel, sentry  # noqa: TC004

__all__ = ["otel", "sentry"]
