# Podman Quadlet deployment

The supplied Spotify and SoundCloud instances run ASGI behind Caddy, using HTTP
over Unix sockets on a Debian host with Podman Quadlet support. Each instance
has a dedicated data owner; the `www-data` group shares only the socket with
Caddy. Native NixOS deployment is described in [docs/nixos.md](../docs/nixos.md).

`latest` is the ASGI image. `asgi` remains a compatibility tag for the same
build. Future releases publish both `<release>` and `<release>-asgi` as ASGI;
historical release tags are unchanged. Pin an image digest in production.

## 1) Prepare directories and settings

```bash
sudo adduser --system --group --home /var/lib/oauthclientbridge/spotify oauthclientbridge-spotify
sudo adduser --system --group --home /var/lib/oauthclientbridge/soundcloud oauthclientbridge-soundcloud

sudo install -d -m 0750 /etc/oauthclientbridge
sudo install -d -m 0755 /etc/oauthclientbridge/spotify /etc/oauthclientbridge/soundcloud
sudo install -d -o root -g root -m 0755 /var/lib/oauthclientbridge
sudo install -d -o oauthclientbridge-spotify -g oauthclientbridge-spotify -m 0700 /var/lib/oauthclientbridge/spotify
sudo install -d -o oauthclientbridge-soundcloud -g oauthclientbridge-soundcloud -m 0700 /var/lib/oauthclientbridge/soundcloud
sudo install -d -o root -g www-data -m 2775 /run/oauthclientbridge/spotify /run/oauthclientbridge/soundcloud

sudo install -m 0600 deploy/spotify/env /etc/oauthclientbridge/spotify/env
sudo install -m 0600 deploy/soundcloud/env /etc/oauthclientbridge/soundcloud/env
sudo install -m 0644 deploy/spotify/callback.html /etc/oauthclientbridge/spotify/callback.html
sudo install -m 0644 deploy/soundcloud/callback.html /etc/oauthclientbridge/soundcloud/callback.html
sudo editor /etc/oauthclientbridge/spotify/env
sudo editor /etc/oauthclientbridge/soundcloud/env
```

Replace `REDACTED` values, including a strong `BRIDGE_SESSION_SECRET`. Adjust
`User=` in each Quadlet to match the service UID and the host's `www-data` GID;
the example `124:33` and `125:33` values are host-specific.

Image defaults are `DB_DATABASE=/data/sqlite.db`,
`BRIDGE_CALLBACK_TEMPLATE_FILE=/config/callback.html`, and
`PROMETHEUS_MULTIPROC_DIR=/run/prom`. The provider env files and callback
templates use matching `csp_nonce` placeholders. Keep the policy and template
in sync; never replace the per-response nonce with a fixed value.

Keep SoundCloud's `OAUTH_REDIRECT_URI=https://www.mopidy.com/soundcloud_callback`:
the website callback forwards into the bridge callback flow.

## 2) Prepare the database

Back up existing SQLite databases before an upgrade, with every writer stopped.
Use `/var/lib/oauthclientbridge/<provider>/sqlite.db` as the persistent path;
do not run two instances against the same file during migration. Data directories
remain private to the service user, not `www-data`.

For a new instance, initialize its database with the selected image and matching
UID/GID (shown for Spotify):

```bash
sudo podman run --rm --read-only --user 124:33 \
  --env-file /etc/oauthclientbridge/spotify/env \
  --tmpfs /run/prom:rw,size=64m,mode=0777 \
  --volume /var/lib/oauthclientbridge/spotify:/data:Z \
  --entrypoint /bin/oauthclientbridge \
  ghcr.io/adamcik/oauthclientbridge:latest initdb
```

Repeat for SoundCloud with its paths and UID. For an existing database, use
`upgradedb` instead of `initdb`. Keep a compatible database backup with the
previous image digest: reverting an image does not revert its database schema.

## 3) Install and start the services

The supplied units use host networking for provider egress; application traffic
uses Unix sockets. Set `Image=` to the selected digest before installation.

```bash
sudo install -d -m 0755 /etc/containers/systemd
sudo install -m 0644 deploy/spotify/container /etc/containers/systemd/oauthclientbridge-spotify.container
sudo install -m 0644 deploy/soundcloud/container /etc/containers/systemd/oauthclientbridge-soundcloud.container
sudo systemctl daemon-reload
sudo systemctl start oauthclientbridge-spotify.service oauthclientbridge-soundcloud.service
sudo systemctl status oauthclientbridge-spotify.service oauthclientbridge-soundcloud.service --no-pager
sudo ls -l /run/oauthclientbridge/spotify/uvicorn.sock /run/oauthclientbridge/soundcloud/uvicorn.sock
```

Quadlet's `[Install]` section arranges startup on boot. The units recreate their
socket directories because `/run` is ephemeral. Containers run read-only, with
bind-mounted data and templates and ephemeral `/tmp` and `/run/prom` tmpfs mounts.

### Replacing an older deployment

Stop old services before starting these units; never share the database with a
parallel canary. Replace `FLASK_SECRET_KEY` and `FLASK_SESSION_COOKIE_*` settings
with `BRIDGE_SESSION_SECRET` and `BRIDGE_SESSION_COOKIE_*`. Remove `THREADS`.
If using the former `*-asgi` canary units, stop and disable them and retire their
Quadlet files before installing the canonical units. Review existing image
drop-ins so an old digest does not override the selected ASGI image.

Flask and ASGI session cookies are not interoperable. Authorization flows already
in progress at cutover must be restarted. Replace Caddy's uWSGI transport with
the HTTP socket configuration below; the current image has no WSGI runtime.

### Graceful shutdown

On `SIGTERM`, Uvicorn stops accepting connections, drains request tasks, and
then runs application cleanup. Keep `ASGI_GRACEFUL_SHUTDOWN_TIMEOUT` greater
than `FETCH_TOTAL_TIMEOUT` and below systemd's `TimeoutStopSec`. The defaults
are 25, 20, and 45 seconds respectively. The image rejects invalid application
timeout ordering at startup.

## 4) Configure Caddy

Save the active Caddy configuration before editing it. The prefix-stripping
`handle_path` routes match the session cookie paths in the provider env files:

```caddy
route {
  redir /spotify /spotify/ 308
  handle_path /spotify/* {
    reverse_proxy unix//run/oauthclientbridge/spotify/uvicorn.sock {
      header_up X-Forwarded-For {remote_host}
      header_up X-Forwarded-Proto {scheme}
      header_up X-Forwarded-Host {host}
      header_up X-Forwarded-Port {server_port}
    }
  }

  redir /soundcloud /soundcloud/ 308
  handle_path /soundcloud/* {
    reverse_proxy unix//run/oauthclientbridge/soundcloud/uvicorn.sock {
      header_up X-Forwarded-For {remote_host}
      header_up X-Forwarded-Proto {scheme}
      header_up X-Forwarded-Host {host}
      header_up X-Forwarded-Port {server_port}
    }
  }

  redir https://www.mopidy.com/
}
```

The ASGI middleware trusts one Caddy hop. Sanitize forwarded headers there and
configure Caddy's trusted proxy chain for the actual topology. Uvicorn's own
proxy-header middleware is disabled to avoid interpreting them twice.

Keep `/metrics` internal: restrict the provider metrics routes to the monitoring
network and set `BRIDGE_METRICS_TOKEN`, or disable them with
`BRIDGE_METRICS_ENABLED=false`.

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl --silent --show-error --output /dev/null --dump-header - 'https://auth.mopidy.com/spotify/?state=verify'
curl --silent --show-error --output /dev/null --dump-header - 'https://auth.mopidy.com/soundcloud/?state=verify'
```

Each request should return `302` with a secure session cookie scoped to its
provider path. Verify authorization, callback, client credentials, and refresh
flows through the public route, then inspect logs and metrics for errors.

## Trace processing with Alloy

`config.alloy` contains the Loki pipeline and an OTLP/HTTP receiver at
`127.0.0.1:4319`. It parses Mopidy-Spotify user agents, batches traces, and
forwards them to Tempo at `127.0.0.1:4318`. The provider env files target Alloy.
Install the configuration and reload Alloy before restarting the applications:

```bash
sudo systemctl reload alloy.service
sudo systemctl restart oauthclientbridge-spotify.service oauthclientbridge-soundcloud.service
sudo journalctl -u alloy.service -n 100 --no-pager
sudo podman logs --tail 100 oauthclientbridge-spotify
sudo podman logs --tail 100 oauthclientbridge-soundcloud
```

## Image validation and upgrades

CI builds and smoke-tests one ASGI image before publishing `latest` and `asgi`
on pushes to `main`; pull requests do not publish images. Release builds publish
the unsuffixed release tag and its `-asgi` compatibility tag from that same image.
The smoke test exercises a disposable database, fake local OAuth provider,
read-only non-root runtime, Unix socket, callback exchange, client credentials,
metrics, routing responses, and graceful shutdown. On Linux with rootless Podman:

```bash
nix run .#image.copyToPodman
nix develop --command deploy/smoke-image.sh
```

`asgi-image` remains a Nix output alias; it now also defaults to the `latest`
tag. Use `SMOKE_IMAGE` to smoke-test a different locally loaded image reference.

`upgrade.sh --instance spotify,soundcloud --check` pulls and compares the selected
image without restarting. Review its `--dry-run` output first. For an approved
upgrade, `--image <digest>` pins a Quadlet drop-in; `--upgrade` also runs
`/bin/oauthclientbridge upgradedb`. Stop writers and back up the database before
using that option. The same CLI provides `cleandb` for periodic maintenance.
