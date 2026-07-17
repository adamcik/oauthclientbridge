from collections.abc import Callable, Mapping
from typing import Any

from flask import Flask
from opentelemetry.trace import Span, TracerProvider

type RequestHook = Callable[[Span, dict[str, Any]], None]
type ResponseHook = Callable[[Span, str, Mapping[str, str] | list[tuple[str, str]]], None]

class FlaskInstrumentor:
    def instrument_app(
        self,
        app: Flask,
        request_hook: RequestHook | None = ...,
        response_hook: ResponseHook | None = ...,
        tracer_provider: TracerProvider | None = ...,
        **kwargs: object,
    ) -> None: ...
