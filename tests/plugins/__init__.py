from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import otel, sentry

__all__ = ["otel", "sentry"]
