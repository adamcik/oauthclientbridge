# Nix packages

`nix build .#web` builds the ASGI server (`bin/oauthclientbridge-web`).
`management` supplies `bin/oauthclientbridge`, including `initdb`, `upgradedb`,
and `cleandb`. Both use the same locked Python environment, including optional
Sentry support. WSGI remains supported only through the existing `image` Docker
image; there is no standalone WSGI package.

`image` and `asgi-image` consume these packages without changing their public
entrypoints, users, settings, or writable mount layout. Their `/bin/entrypoint`
wrappers retain container-specific behavior, including the ASGI socket umask.

`assets` provides `share/oauthclientbridge/templates/{spotify,soundcloud}.html`
and `share/oauthclientbridge/static`. The public static directory supplies a
`robots.txt` excluding this authentication service from crawlers; there are no
frontend bundles. Hosts may serve it directly without a Python request. Never
expose the templates directory through a file server. Callback templates must be
rendered by the application because they contain per-request credentials and state.

Both images include the asset package. Native hosts can select its templates
with `BRIDGE_CALLBACK_TEMPLATE_FILE` and point their existing file server at
the static directory. Keep web, management, and assets from the same flake
revision; do not copy mutable assets into a separate deployment directory.
