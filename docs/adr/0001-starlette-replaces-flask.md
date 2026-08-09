# Starlette Replaces Flask

OAuth Client Bridge will transition from Flask/WSGI to a Starlette-only ASGI production runtime. Flask remains the sole production runtime while bridge routes are characterized and ported through shared asynchronous Bridge logic, invoked through a temporary synchronous adapter; it is removed only after Starlette is functionally conformant and the ASGI deployment replaces uWSGI and Caddy's uWSGI socket contract.

Conformance does not require duplicate OAuth parameters to use the same value precedence in both frameworks. Cutover and rollback also do not preserve an authorization flow already in progress because Flask and Starlette session cookies are intentionally not interoperable; affected callbacks safely fail with `invalid_state` and can be restarted.
