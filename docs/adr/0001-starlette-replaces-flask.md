# Starlette Replaces Flask

OAuth Client Bridge supports a Starlette-only ASGI production runtime. Shared asynchronous Bridge logic owns OAuth behavior; Starlette adapts HTTP requests and responses. Flask/WSGI and Caddy's uWSGI socket contract are no longer supported.

The migration did not require duplicate OAuth parameters to use the same value precedence in both frameworks. Switching between historical Flask releases and ASGI also does not preserve an authorization flow already in progress because their session cookies are intentionally not interoperable; affected callbacks safely fail with `invalid_state` and can be restarted.
