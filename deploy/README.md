# Podman + systemd Deployment (Current Migration State)

This is the current main deployment procedure for this host setup:

- Debian 11 (bullseye)
- Podman 3.x (no Quadlet support)
- Caddy speaking uWSGI over unix sockets
- container runtime user `www-data` (`33:33`)

Note: this flow uses `podman create` + `podman generate systemd --new`.

## Files in this directory

- `spotify/env`
- `spotify/callback.html`
- `soundcloud/env`
- `soundcloud/callback.html`

## 1) Prepare host dirs and env files

```bash
sudo install -d -m 0750 /etc/oauthclientbridge
sudo install -d -m 0755 /etc/oauthclientbridge/templates

sudo install -d -o www-data -g www-data -m 0750 /var/lib/oauthclientbridge
sudo install -d -o www-data -g www-data -m 0750 /var/lib/oauthclientbridge/spotify
sudo install -d -o www-data -g www-data -m 0750 /var/lib/oauthclientbridge/soundcloud

sudo install -d -o root -g www-data -m 2775 /run/oauthclientbridge/spotify
sudo install -d -o root -g www-data -m 2775 /run/oauthclientbridge/soundcloud

sudo cp deploy/spotify/env /etc/oauthclientbridge/spotify.env
sudo cp deploy/soundcloud/env /etc/oauthclientbridge/soundcloud.env

sudo cp deploy/spotify/callback.html /etc/oauthclientbridge/templates/spotify-callback.html
sudo cp deploy/soundcloud/callback.html /etc/oauthclientbridge/templates/soundcloud-callback.html

sudo editor /etc/oauthclientbridge/spotify.env
sudo editor /etc/oauthclientbridge/soundcloud.env
```

Ensure both env files set:

- `PROMETHEUS_MULTIPROC_DIR=/prom`

SoundCloud note:

- Keep `OAUTH_REDIRECT_URI=https://www.mopidy.com/soundcloud_callback`.
- This legacy redirect is intentional; website callback forwards into auth bridge callback flow.

## 2) Migrate DB files from legacy `/srv` (one-time)

```bash
sudo systemctl stop oauthclientbridge-spotify.service oauthclientbridge-soundcloud.service || true

sudo cp -a /srv/virtualenvs/oauthclientbridge/run/spotify.db* /var/lib/oauthclientbridge/spotify/
sudo cp -a /srv/virtualenvs/oauthclientbridge/run/soundcloud.db* /var/lib/oauthclientbridge/soundcloud/

sudo chown -R www-data:www-data /var/lib/oauthclientbridge/spotify
sudo chown -R www-data:www-data /var/lib/oauthclientbridge/soundcloud
```

Keep `/srv/virtualenvs/oauthclientbridge/run` temporarily for rollback, but do not dual-write both locations.

## 3) Create containers (host network)

Why host network here: Podman 3 + bullseye bridge networking can fail outbound provider access.
Host networking avoids that and is acceptable here because app traffic is via unix sockets.

```bash
sudo podman rm -f oauthclientbridge-spotify oauthclientbridge-soundcloud || true
sudo podman pull ghcr.io/adamcik/oauthclientbridge:latest

sudo podman create \
  --name oauthclientbridge-spotify \
  --network host \
  --user 33:33 \
  --env-file /etc/oauthclientbridge/spotify.env \
  -v /var/lib/oauthclientbridge/spotify:/data \
  -v /run/oauthclientbridge/spotify:/run/uwsgi \
  -v /etc/oauthclientbridge/templates:/config:ro \
  --tmpfs /prom:rw,size=64m,mode=1777 \
  ghcr.io/adamcik/oauthclientbridge:latest \
  --socket /run/uwsgi/uwsgi.sock --chmod-socket=660 --vacuum

sudo podman create \
  --name oauthclientbridge-soundcloud \
  --network host \
  --user 33:33 \
  --env-file /etc/oauthclientbridge/soundcloud.env \
  -v /var/lib/oauthclientbridge/soundcloud:/data \
  -v /run/oauthclientbridge/soundcloud:/run/uwsgi \
  -v /etc/oauthclientbridge/templates:/config:ro \
  --tmpfs /prom:rw,size=64m,mode=1777 \
  ghcr.io/adamcik/oauthclientbridge:latest \
  --socket /run/uwsgi/uwsgi.sock --chmod-socket=660 --vacuum
```

## 4) Generate and install systemd units

```bash
sudo podman generate systemd --name oauthclientbridge-spotify --files --new
sudo podman generate systemd --name oauthclientbridge-soundcloud --files --new

sudo install -D -m 0644 container-oauthclientbridge-spotify.service /etc/systemd/system/oauthclientbridge-spotify.service
sudo install -D -m 0644 container-oauthclientbridge-soundcloud.service /etc/systemd/system/oauthclientbridge-soundcloud.service
```

## 5) Add path/permission ExecStartPre drop-ins

```bash
sudo mkdir -p /etc/systemd/system/oauthclientbridge-spotify.service.d
sudo tee /etc/systemd/system/oauthclientbridge-spotify.service.d/paths.conf >/dev/null <<'EOF'
[Service]
ExecStartPre=/usr/bin/install -d -o www-data -g www-data -m 0750 /var/lib/oauthclientbridge/spotify
ExecStartPre=/usr/bin/install -d -o root -g www-data -m 2775 /run/oauthclientbridge/spotify
EOF

sudo mkdir -p /etc/systemd/system/oauthclientbridge-soundcloud.service.d
sudo tee /etc/systemd/system/oauthclientbridge-soundcloud.service.d/paths.conf >/dev/null <<'EOF'
[Service]
ExecStartPre=/usr/bin/install -d -o www-data -g www-data -m 0750 /var/lib/oauthclientbridge/soundcloud
ExecStartPre=/usr/bin/install -d -o root -g www-data -m 2775 /run/oauthclientbridge/soundcloud
EOF
```

## 6) Start and enable services

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oauthclientbridge-spotify.service oauthclientbridge-soundcloud.service
```

## 7) Verify

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

## 8) Caddy canary routing (parallel migration)

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
        transport uwsgi
      }
    }

    redir /soundcloud /soundcloud/ 308
    handle_path /soundcloud/* {
      reverse_proxy unix//run/oauthclientbridge/soundcloud/uwsgi.sock {
        transport uwsgi
      }
    }
  }

  rewrite /spotify /spotify/
  reverse_proxy /spotify/* unix//run/uwsgi/app/auth-spotify/socket {
    transport uwsgi
  }

  rewrite /soundcloud /soundcloud/
  reverse_proxy /soundcloud/* unix//run/uwsgi/app/auth-soundcloud/socket {
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

- Image entrypoint does not implicitly bind an HTTP port. Listener mode is set explicitly
  via container args (for example `--socket ...` in this deployment).
- `tmpfs /prom` is intentionally ephemeral to avoid stale Prometheus multiprocess files.
- Secrets are currently mixed into env files; move to sops-managed env files later if desired.
