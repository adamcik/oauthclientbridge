Prefer smallest agreed slice first; discuss uncertain refactors before editing.

Treat submodules of packages/modules with a curated `__all__` as internal implementation details. Production imports should come from the package surface. In tests, direct module imports are fine when the test is focused on that same module.

Prefer `pytest -q` for lower-noise test runs unless fuller output is needed.

Prefer module imports when they improve readability by preserving context, e.g. `oauth.normalize_error(...)` over importing several helpers directly.

Prefer `@pytest.mark.parametrize()` scenario/case DTOs with ids or names and keyword-style fields over positional tuple cases when that improves readability.

When working through review feedback, pick one agreed change, make the smallest correct edit, verify it, and keep commits atomic.
