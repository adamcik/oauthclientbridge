# Native NixOS deployment

Import `inputs.oauthclientbridge.nixosModules.default`. Containers remain supported;
the native service runs the same packaged ASGI entrypoint and CLI as `asgi-image`.
The native module does not configure a database server, proxy, TLS, firewall, or
backups. SQLite is the app's existing database, not a new deployment dependency.

```nix
{
  imports = [ inputs.oauthclientbridge.nixosModules.default ];
  services.oauthclientbridge.instances = {
    spotify = {
      environmentFile = "/run/secrets/oauthclientbridge-spotify";
      port = 8001;
      workers = 4;
      environment = {
        OAUTH_CLIENT_ID = "public-client-id";
        OAUTH_AUTHORIZATION_URI = "https://accounts.spotify.com/authorize";
        OAUTH_TOKEN_URI = "https://accounts.spotify.com/api/token";
        OAUTH_REDIRECT_URI = "https://auth.example.com/callback";
      };
      # Opt in only after reviewing database ownership and maintenance policy.
      cleanDatabase.enable = false;
    };
  };
}
```

Instance names are 1–12 lowercase letters, digits, or hyphens, starting with a
letter. Each gets a dedicated `oauthclientbridge-NAME` user and group, a private
`/var/lib/oauthclientbridge-NAME` state directory, and a private
`/run/oauthclientbridge-NAME` runtime directory. Ports must be distinct. HTTP
listens only on `127.0.0.1`; use the host's existing proxy to provide HTTPS.
`gracefulShutdownTimeout` defaults to 25 seconds; systemd allows 10 more seconds
before forcing shutdown. Units restrict privileges, devices, writable paths,
address families, and system calls without blocking outbound provider requests.

Provision the runtime environment file through the host's existing secret
manager, owned by root with mode `0600`. It uses systemd EnvironmentFile syntax,
not shell syntax (`KEY=value`, no `export`). Include at least:

```text
OAUTH_CLIENT_SECRET=provider-secret
BRIDGE_SESSION_SECRET=long-random-session-secret
```

Never put secret values in `environment`, a Nix string literal, or a Nix path:
these enter the world-readable store. The file must exist before any web or
management unit starts. Its values override `environment`. Database and metrics
paths are module-owned and enforced after loading the file. Use existing app
settings for optional Sentry, telemetry, scopes, metrics authorization, and
callback rendering. Changes to file contents require an explicit service restart;
Nix cannot track runtime secret rotations. Processes necessarily receive secrets
in their environment, so privileged host administrators can inspect them.

## Provisioning and migrations are explicit

Rebuilds and web startup **never run migrations**. A new instance cannot serve
requests until an operator initializes its database:

```sh
sudo systemctl start oauthclientbridge-spotify-initdb.service
sudo systemctl start oauthclientbridge-spotify.service
```

The first rebuild may show a failing web unit until this initialization is done.
`initdb` is for new databases; for an existing database use `upgradedb` after
backing it up. The management units use the same identity, environment, package,
and state directory as the web service. Starting either migration unit explicitly
stops that instance's web service; it does not restart it afterward.

For a native upgrade, stop its cleanup timer as well as web before taking the
backup. Review activation ordering: a NixOS switch can restart web with the new
package, but cannot establish database compatibility for you. Keep incompatible
versions out of traffic until the explicit migration succeeds, then restart web
and any previously enabled timer. Do not run migrations while other writers or
maintenance jobs are active.

To move a container instance:

1. Pin the source and target app revisions. Do not simultaneously change runtime,
   schema, and provider settings without a tested recovery plan.
2. Stop its container and any scheduled jobs. Never run both deployment paths
   against a copied or shared writable database during the cutover.
3. Take a consistent SQLite backup using the host's backup procedure. Copying only
   the main file while WAL writes are active can lose committed data. Quiesce all
   writers and checkpoint, or use SQLite's backup API. Preserve the original.
4. Install the copy as `/var/lib/oauthclientbridge-spotify/sqlite.db`, owned by
   `oauthclientbridge-spotify:oauthclientbridge-spotify`, mode `0600`; keep its
   parent directory mode `0700`. Keep the host's backup configuration responsible
   for this new path.
5. Install equivalent runtime settings and secrets. Preserve provider redirect
   URIs, cookie settings, callback template, scopes, and telemetry configuration.
6. Explicitly start `oauthclientbridge-spotify-upgradedb.service` if required by
   the target version. Check its journal and exit status before starting web.
7. Start web, verify application behavior, then change the host's proxy route.
   Retain the old image digest and backup until the rollback window closes.

`cleanDatabase.enable` defaults to false. Enabling it creates a persistent weekly
timer (with up to 15 minutes of jitter) running `cleandb`, which vacuums SQLite;
`cleanDatabase.onCalendar` customizes the schedule. Vacuuming may contend with
traffic, so choose a maintenance window. The native module deliberately supports
isolated databases only, not shared state paths. If existing containers or other
writers share a database, keep their jobs disabled until exactly one maintenance
owner has been selected. Do not enable one job per replica of a shared database.

## Proxy and static files

Configure the host's existing proxy to connect to the chosen loopback port. It
must replace forwarded headers from untrusted clients and supply the intended
scheme, host, port, and client address. Loopback trust is a host security boundary;
do not let untrusted local processes act as proxies. Review the app's existing
metrics access policy and expose only the routes intended for public use.

`services.oauthclientbridge.instances.spotify.staticRoot` is the matching store
directory for the host's file server. It currently contains only `robots.txt`;
serve `/robots.txt` from that directory without routing through Python. No mutable
collect-static step or frontend build is required. See [Nix packages](nix-packages.md)
for template paths. Templates are **not** static responses: never expose the
templates directory, and do not serve callback credentials from the store.

## Rollback

**NixOS rollback does not roll back the database.** It restores an older system
and app closure, not SQLite contents, schema changes, tokens, or runtime secrets.
Before upgrading, test whether the older app can read the newer schema. If not,
rollback requires stopping every writer and restoring a compatible database
backup, with the expected loss of changes since that backup. Do not blindly
re-run `initdb` or copy a database over live WAL files. Switching the proxy back
to a container is safe only when that container uses a database compatible with
its pinned image; avoid resuming writes to an obsolete pre-cutover copy.

## Verification

Run `nix fmt`, `nix flake check`, and
`nix build .#image .#asgi-image`. On Linux, flake checks include the VM test;
run it alone with `nix build -L .#checks.x86_64-linux.nixos`.
It boots NixOS with the real packaged app, SQLite, two isolated instances,
runtime secret files, explicit initialization and legacy migration, an opt-in
timer, and a host-configured nginx serving store assets. A local fake OAuth
provider avoids real credentials and external network dependencies; it does not
test provider availability. Existing rootless Podman smoke tests remain useful
for container-specific behavior (see `deploy/README.md`).
