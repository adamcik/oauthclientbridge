# Podman Quadlet Deployment

This is the current deployment procedure for this host setup:

- Debian
- Podman with Quadlet support
- Caddy speaking uWSGI over unix sockets
- container runtime user `www-data` (`33:33`)

This flow installs per-instance quadlets into `/etc/containers/systemd/`.

## Files in this directory

- `spotify/container`
- `spotify/env`
- `spotify/callback.html`
- `soundcloud/container`
- `soundcloud/env`
- `soundcloud/callback.html`
- `config.alloy`

## 1) Prepare host dirs and env files

```bash
sudo adduser --system --group --home /var/lib/oauthclientbridge/spotify oauthclientbridge-spotify
sudo adduser --system --group --home /var/lib/oauthclientbridge/soundcloud oauthclientbridge-soundcloud

sudo install -d -m 0750 /etc/oauthclientbridge
sudo install -d -m 0755 /etc/oauthclientbridge/spotify
sudo install -d -m 0755 /etc/oauthclientbridge/soundcloud

sudo install -d -o root -g root -m 0755 /var/lib/oauthclientbridge
sudo install -d -o oauthclientbridge-spotify -g oauthclientbridge-spotify -m 0700 /var/lib/oauthclientbridge/spotify
sudo install -d -o oauthclientbridge-soundcloud -g oauthclientbridge-soundcloud -m 0700 /var/lib/oauthclientbridge/soundcloud

sudo install -d -o root -g www-data -m 2775 /run/oauthclientbridge/spotify
sudo install -d -o root -g www-data -m 2775 /run/oauthclientbridge/soundcloud

sudo cp deploy/spotify/env /etc/oauthclientbridge/spotify/env
sudo cp deploy/soundcloud/env /etc/oauthclientbridge/soundcloud/env

sudo cp deploy/spotify/callback.html /etc/oauthclientbridge/spotify/callback.html
sudo cp deploy/soundcloud/callback.html /etc/oauthclientbridge/soundcloud/callback.html

sudo editor /etc/oauthclientbridge/spotify/env
sudo editor /etc/oauthclientbridge/soundcloud/env
```

Image defaults (via nix2container) pin these settings:

- `DB_DATABASE=/data/sqlite.db`
- `BRIDGE_CALLBACK_TEMPLATE_FILE=/config/callback.html`
- `PROMETHEUS_MULTIPROC_DIR=/run/prom`

Use per-instance env files for secrets/provider settings; only set these in env files when intentionally overriding defaults.

SoundCloud note:

- Keep `OAUTH_REDIRECT_URI=https://www.mopidy.com/soundcloud_callback`.
- This legacy redirect is intentional; website callback forwards into auth bridge callback flow.

## 2) Migrate DB files from legacy `/srv` (one-time)

```bash
sudo systemctl stop oauthclientbridge-spotify.service oauthclientbridge-soundcloud.service || true

sudo install -D -o oauthclientbridge-spotify -g oauthclientbridge-spotify -m 0600 /srv/virtualenvs/oauthclientbridge/run/spotify.db /var/lib/oauthclientbridge/spotify/sqlite.db
sudo install -D -o oauthclientbridge-soundcloud -g oauthclientbridge-soundcloud -m 0600 /srv/virtualenvs/oauthclientbridge/run/soundcloud.db /var/lib/oauthclientbridge/soundcloud/sqlite.db

sudo chown -R oauthclientbridge-spotify:oauthclientbridge-spotify /var/lib/oauthclientbridge/spotify
sudo chown -R oauthclientbridge-soundcloud:oauthclientbridge-soundcloud /var/lib/oauthclientbridge/soundcloud
```

Keep `/srv/virtualenvs/oauthclientbridge/run` temporarily for rollback, but do not dual-write both locations.

The data directories are intentionally private to the container user. `www-data` only needs access to `/run/oauthclientbridge/<instance>` for socket sharing with Caddy, not to `/var/lib/oauthclientbridge/<instance>`.

## 3) Install quadlets (host network)

Why host network here: Podman 3 + bullseye bridge networking can fail outbound provider access.
Host networking avoids that and is acceptable here because app traffic is via unix sockets.

```bash
sudo install -d -m 0755 /etc/containers/systemd
sudo install -D -m 0644 deploy/spotify/container /etc/containers/systemd/oauthclientbridge-spotify.container
sudo install -D -m 0644 deploy/soundcloud/container /etc/containers/systemd/oauthclientbridge-soundcloud.container
```

If migrating from legacy non-quadlet units, remove the old unit files before reloading systemd:

```bash
sudo systemctl disable --now oauthclient-spotify.service || true
sudo systemctl disable --now oauthclient-soundcloud.service || true
sudo rm -f /etc/systemd/system/oauthclient-spotify.service
sudo rm -f /etc/systemd/system/oauthclient-soundcloud.service
```

## 4) Start and enable services

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oauthclientbridge-spotify.service oauthclientbridge-soundcloud.service
```

Quadlet will materialize the generated service units from the `.container` files on reload.

## Trace Processing With Alloy

Install `deploy/config.alloy` as the host Alloy configuration. It contains the
Loki pipeline and an OTLP/HTTP trace pipeline that receives application traces
on `127.0.0.1:4319`, parses Mopidy-Spotify user agents on request spans, batches
the traces, and forwards them to Tempo's existing local receiver on
`127.0.0.1:4318`.

The Spotify and SoundCloud environment files use the Alloy receiver endpoint.
After installing the Alloy configuration, reload Alloy before restarting either
application:

```bash
sudo systemctl reload alloy.service
sudo systemctl restart oauthclientbridge-spotify.service oauthclientbridge-soundcloud.service
```

Confirm Alloy is accepting traces and Tempo continues receiving them:

```bash
sudo systemctl status alloy.service --no-pager
sudo journalctl -u alloy.service -n 100 --no-pager
```

## 5) Verify

```bash
sudo systemctl status oauthclientbridge-spotify.service --no-pager
sudo systemctl status oauthclientbridge-soundcloud.service --no-pager
sudo podman ps --filter name=oauthclientbridge
sudo ls -l /run/oauthclientbridge/spotify/uwsgi.sock /run/oauthclientbridge/soundcloud/uwsgi.sock

sudo podman logs --tail 100 oauthclientbridge-spotify
sudo podman logs --tail 100 oauthclientbridge-soundcloud
```

Optional provider egress check from each container net namespace:

```bash
sudo podman run --rm --network container:oauthclientbridge-spotify docker.io/curlimages/curl:8.8.0 -4 --connect-timeout 5 --max-time 12 -sS -o /dev/null -w 'spotify v4=%{http_code}\n' https://accounts.spotify.com/api/token
sudo podman run --rm --network container:oauthclientbridge-soundcloud docker.io/curlimages/curl:8.8.0 -4 --connect-timeout 5 --max-time 12 -sS -o /dev/null -w 'soundcloud v4=%{http_code}\n' https://api.soundcloud.com
```

`401`/`405` is fine here (proves outbound network path works).

## 6) Caddy canary routing (parallel migration)

Socket paths:

- Spotify: `/run/oauthclientbridge/spotify/uwsgi.sock`
- SoundCloud: `/run/oauthclientbridge/soundcloud/uwsgi.sock`

Use canary match on your own source IP(s), route only canary to new sockets,
keep legacy upstreams for everyone else.

```caddy
@canary remote_ip <ips>

route {
  handle @canary {
    redir /spotify /spotify/ 308

    handle_path /spotify/* {
      reverse_proxy unix//run/oauthclientbridge/spotify/uwsgi.sock {
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
        header_up X-Forwarded-Host {host}
        header_up X-Forwarded-Port {server_port}
        transport uwsgi
      }
    }

    redir /soundcloud /soundcloud/ 308
    handle_path /soundcloud/* {
      reverse_proxy unix//run/oauthclientbridge/soundcloud/uwsgi.sock {
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
        header_up X-Forwarded-Host {host}
        header_up X-Forwarded-Port {server_port}
        transport uwsgi
      }
    }
  }

  rewrite /spotify /spotify/
  reverse_proxy /spotify/* unix//run/uwsgi/app/auth-spotify/socket {
    header_up X-Forwarded-For {remote_host}
    header_up X-Forwarded-Proto {scheme}
    header_up X-Forwarded-Host {host}
    header_up X-Forwarded-Port {server_port}
    transport uwsgi
  }

  rewrite /soundcloud /soundcloud/
  reverse_proxy /soundcloud/* unix//run/uwsgi/app/auth-soundcloud/socket {
    header_up X-Forwarded-For {remote_host}
    header_up X-Forwarded-Proto {scheme}
    header_up X-Forwarded-Host {host}
    header_up X-Forwarded-Port {server_port}
    transport uwsgi
  }

  redir https://www.mopidy.com/
}
```

Then apply:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## Notes

- The Caddy uWSGI transport sends Caddy's request host and remote address
  directly; uWSGI derives the request scheme from `X-Forwarded-Proto`. Configure
  Caddy's trusted proxy chain correctly and sanitize forwarded headers there.
- Keep `/metrics` internal. The application disables it by default; when it is
  enabled, configure `BRIDGE_METRICS_TOKEN` and additionally restrict the Caddy
  route to the monitoring network.
- Image entrypoint does not implicitly bind an HTTP port. Listener mode is set explicitly
  via container args (for example `--socket ...` in this deployment).
- Containers run with `--read-only`; writable paths are provided via bind mounts and tmpfs.
- `tmpfs /run/prom` is intentionally ephemeral to avoid stale Prometheus multiprocess files.
- Secrets are currently mixed into env files; move to sops-managed env files later if desired.
