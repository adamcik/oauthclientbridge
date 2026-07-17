from collections.abc import Callable
from typing import Any

from opentelemetry.trace import Span

type LogHook = Callable[[Span, object], None]

class LoggingInstrumentor:
    def instrument(self, **kwargs: Any) -> None: ...
    def uninstrument(self, **kwargs: Any) -> None: ...
