# Request Lifecycle Observer

Flask and Starlette share an explicit Request Lifecycle Observer for sanitized
request context, access logs, and Prometheus request metrics. When tracing is
enabled, framework instrumentation creates propagated server traces, while
OAuth Outcome and Fallback Observers retain their separate logical-outcome and
exception-capture responsibilities. Both adapters emit the same lifecycle
fields and use the stable Endpoint vocabulary for metrics, with framework-native
unmatched requests labeled `unknown`, before the ASGI production cutover.
