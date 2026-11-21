# Nix packages

`nix build .#web` builds the ASGI server (`bin/oauthclientbridge-web`).
`management` supplies `bin/oauthclientbridge`, including `initdb`, `upgradedb`,
and `cleandb`. Both use the same locked Python environment, including optional
Sentry support.

`image` builds the ASGI container tagged `latest`; `asgi-image` is an alias for
the same build, including its default tag. CI also publishes it as `asgi` and
publishes future releases as both `<release>` and `<release>-asgi`. Existing
historical release tags are not changed. Both output names consume the native
packages; `/bin/entrypoint` retains the container-specific ASGI socket umask.
To copy the compatibility tag locally, use
`nix run .#asgi-image.copyTo -- containers-storage:ghcr.io/adamcik/oauthclientbridge:asgi`.

`assets` provides `share/oauthclientbridge/templates/{spotify,soundcloud}.html`
and `share/oauthclientbridge/static`. The public static directory supplies a
`robots.txt` excluding this authentication service from crawlers; there are no
frontend bundles. Hosts may serve it directly without a Python request. Never
expose the templates directory through a file server. Callback templates must be
rendered by the application because they contain per-request credentials and state.

The image includes the asset package. Native hosts can select its templates
with `BRIDGE_CALLBACK_TEMPLATE_FILE` and point their existing file server at
the static directory. Keep web, management, and assets from the same flake
revision; do not copy mutable assets into a separate deployment directory.
