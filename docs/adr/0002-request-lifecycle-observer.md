# Request Lifecycle Observer

Starlette uses an explicit Request Lifecycle Observer for sanitized
request context, access logs, and Prometheus request metrics. When tracing is
enabled, framework instrumentation creates propagated server traces, while
OAuth Outcome and Fallback Observers retain their separate logical-outcome and
exception-capture responsibilities. The adapter uses the stable Endpoint
vocabulary for metrics, with framework-native unmatched requests labeled
`unknown`.
