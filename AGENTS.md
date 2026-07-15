Prefer smallest agreed slice first; discuss uncertain refactors before editing.

Prefer importing a module or package and qualifying its names when that preserves ownership and context, for example `oauth.normalize_error(...)` rather than importing several helpers directly.

Packages with a curated `__all__` expose the sole production import surface. Code outside such a package must import its facade, not its implementation modules. For example, use `from oauthclientbridge import telemetry` and `telemetry.init_tracing(...)`, never `from oauthclientbridge.telemetry import _otel`.

Use underscore-prefixed implementation module names below curated packages, such as `telemetry._otel`, `telemetry._prometheus`, and `telemetry._resources`. They are private and may change without notice.

Prefer tests that verify observable behavior through the public API. Tests may import a private implementation module only when directly testing it; avoid excessive internal assertions and mocks. Repeated pressure to reach internals is a signal to improve the public API, even when that requires a refactor.

Prefer `pytest -q` for lower-noise test runs unless fuller output is needed.

Prefer `@pytest.mark.parametrize()` scenario/case DTOs with ids or names and keyword-style fields over positional tuple cases when that improves readability.

When working through review feedback, pick one agreed change, make the smallest correct edit, verify it, and keep commits atomic.
