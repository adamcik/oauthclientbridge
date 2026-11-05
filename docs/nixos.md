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
      credentials = {
        OAUTH_CLIENT_SECRET.file = "/run/secrets/spotify-client-secret";
        BRIDGE_SESSION_SECRET = {
          file = "/run/credstore.encrypted/spotify-session.cred";
          encrypted = true;
        };
      };
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
`/run/oauthclientbridge-NAME` runtime directory. Each instance listens on the HTTP
Unix socket `/run/oauthclientbridge-NAME.sock`, owned by its dedicated user and
group with mode `0660`. There is no native TCP listener or port option. systemd
owns the socket and passes it to Uvicorn; it survives web restarts and can activate
web on demand. State and metrics directories remain mode `0700`.
`gracefulShutdownTimeout` defaults to 25 seconds; systemd allows 10 more seconds
before forcing shutdown. Units restrict privileges, devices, writable paths,
address families, and system calls without blocking outbound provider requests.

## Runtime secrets through systemd credentials

`credentials` maps full application setting names to runtime source files.
systemd uses `LoadCredential` by default, or `LoadCredentialEncrypted` when
`encrypted = true`. Each web, migration, and cleanup unit gets its own protected
credential directory. The app reads files from `CREDENTIALS_DIRECTORY` directly;
credential contents are not copied into process environment variables or command
lines. Existing mount hardening also isolates each unit's credential directory.

Provision source files through the host's existing secret manager; keep plaintext
sources owned by root with mode `0600`. Paths must be absolute strings outside
the Nix store, not Nix path literals. Plaintext files
contain one setting's UTF-8 value, without `KEY=` or shell syntax. Pydantic trims
surrounding whitespace; non-string settings use the same value format as their
environment counterparts. At minimum, provide `OAUTH_CLIENT_SECRET` and
`BRIDGE_SESSION_SECRET`.

Encrypted files must carry the same credential name as the option key. For
example, on the target host:

```sh
systemd-creds encrypt --name=BRIDGE_SESSION_SECRET session-secret.txt spotify-session.cred
```

This is an operator step, not a build or activation hook. Choose the host's
TPM/host-key policy and protect the plaintext input through its existing secret
management procedure. Encrypted blobs can be bound to the host key and/or TPM;
do not assume copying them to another machine makes them usable there. See
[systemd credentials](https://systemd.io/CREDENTIALS/) for encryption policy and
backup implications. This module does not provision keys, a TPM, or a secret manager.

Missing or undecryptable configured credential files prevent unit activation.
Credentials are acquired at each activation and remain immutable while the unit
runs. After rotating a source file, explicitly restart web; the next migration
or scheduled job reads its own fresh copy. Nix cannot track runtime file contents.
Do not inspect credentials by printing their contents into journals or diagnostics.

### Existing environment-file deployments

`environmentFile` remains optional and backward compatible. Provision it through
the host's existing secret manager, owned by root with mode `0600`. It uses
systemd EnvironmentFile syntax, not shell syntax (`KEY=value`, no `export`):

```text
OAUTH_CLIENT_SECRET=provider-secret
BRIDGE_SESSION_SECRET=long-random-session-secret
```

Never put secret values in `environment`, a Nix string literal, or a Nix path:
these enter the world-readable store. A configured environment file must exist
before any web or management unit starts. Its values override `environment`, and
both take precedence over credential files. Remove old secret assignments when
switching to credentials, otherwise they continue to win. Explicit values passed
to the Python settings constructor retain their existing highest precedence.

Environment-file secrets still enter the process environment; prefer credentials
for new deployments. Database and metrics paths and `CREDENTIALS_DIRECTORY` are
module-owned. Use the normal app settings for optional Sentry, telemetry, scopes,
metrics authorization, and callback rendering. Out-of-band environment-file
changes also require an explicit service restart.

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
stops that instance's web service and socket listener; it does not restart them
afterward. Stopping the listener prevents incoming requests from reactivating web
during migration. Starting web afterward also starts its listener.

For a native upgrade, stop its socket listener, web, and any enabled cleanup timer
before taking the backup. Stopping only web permits socket traffic to reactivate
it. Review activation ordering: a NixOS switch can restart web with the new
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
5. Install equivalent runtime settings and secret files. Preserve provider redirect
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

Configure the host's existing proxy to speak HTTP over
`services.oauthclientbridge.instances.spotify.socketPath`. Grant only the proxy
user membership in the corresponding instance group, for example:

```nix
users.users.nginx.extraGroups = [ "oauthclientbridge-spotify" ];
```

This grants socket access, not access to the private state or metrics directories.
For nginx, an upstream server address is
`unix:/run/oauthclientbridge-spotify.sock`; this is HTTP, not the uWSGI protocol.
The host still owns proxy routing and TLS. The proxy must replace forwarded
headers from untrusted clients and supply the intended scheme, host, port, and
client address. Socket access is a trust boundary: only trusted proxy users should
join these groups. Review the app's existing metrics access policy and expose only
the routes intended for public use.

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
plaintext and host-key-encrypted credentials, explicit initialization and legacy migration, an opt-in
timer, and a host-configured nginx serving store assets. It verifies Unix socket
permissions, proxy-group access, denied cross-instance access, no app TCP listener,
socket activation after web shutdown, and listener shutdown during migration.
It verifies credential delivery to web and management units, a credential-only
instance without an environment file, cross-instance denial, and absence of
secret values from process environments.
A local fake OAuth provider avoids real credentials and external network
dependencies; it does not test provider availability. Existing rootless Podman
smoke tests remain useful for container-specific behavior (see `deploy/README.md`).
