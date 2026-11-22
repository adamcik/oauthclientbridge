# Nix packages

`nix build .#web` builds the ASGI server (`bin/oauthclientbridge-web`).
`management` supplies `bin/oauthclientbridge`, including `initdb`, `upgradedb`,
and `cleandb`. Both use the same locked Python environment, including optional
Sentry support.

`default` selects `web`. Deploy these packages through the
[NixOS module](nixos.md); container image outputs and publishing are not supported.

`assets` provides `share/oauthclientbridge/templates/{spotify,soundcloud}.html`
and `share/oauthclientbridge/static`. The public static directory supplies a
`robots.txt` excluding this authentication service from crawlers; there are no
frontend bundles. Hosts may serve it directly without a Python request. Never
expose the templates directory through a file server. Callback templates must be
rendered by the application because they contain per-request credentials and state.

Native hosts can select the asset package's templates
with `BRIDGE_CALLBACK_TEMPLATE_FILE` and point their existing file server at
the static directory. Keep web, management, and assets from the same flake
revision; do not copy mutable assets into a separate deployment directory.
