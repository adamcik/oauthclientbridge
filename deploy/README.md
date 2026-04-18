# Podman Quadlet Deployment (Spotify + SoundCloud)

This setup assumes:

- Quadlet (`podman-system-generator`)
- Caddy speaking uWSGI over unix sockets
- current runtime model: container runs as `www-data` (`33:33`)

Note: in current deployment files, each `*.env` contains both non-secret and
secret values. Move secrets to sops-managed files later if desired.

## Files in this directory

- `spotify/container`
- `spotify/env`
- `spotify/callback.html`
- `soundcloud/container`
- `soundcloud/env`
- `soundcloud/callback.html`

## 1) Prepare host dirs and env files

```bash
sudo install -d -m 0750 /etc/oauthclientbridge
sudo install -d -m 0755 /etc/oauthclientbridge/templates
sudo install -d -o www-data -g www-data -m 0750 /srv/virtualenvs/oauthclientbridge/run
sudo install -d -o root -g www-data -m 2775 /run/oauthclientbridge/spotify
sudo install -d -o root -g www-data -m 2775 /run/oauthclientbridge/soundcloud

sudo cp deploy/spotify/env /etc/oauthclientbridge/spotify.env
sudo cp deploy/soundcloud/env /etc/oauthclientbridge/soundcloud.env

sudo editor /etc/oauthclientbridge/spotify.env
sudo editor /etc/oauthclientbridge/soundcloud.env
```

Ensure both env files set:

- `PROMETHEUS_MULTIPROC_DIR=/prom`

SoundCloud note:

- Keep `OAUTH_REDIRECT_URI=https://www.mopidy.com/soundcloud_callback`.
- This legacy redirect is intentional; the website callback forwards into the
  auth bridge callback flow.

Place callback templates on host:

```bash
sudo cp deploy/spotify/callback.html /etc/oauthclientbridge/templates/spotify-callback.html
sudo cp deploy/soundcloud/callback.html /etc/oauthclientbridge/templates/soundcloud-callback.html
```

## 2) Ensure existing DB files are writable by runtime uid/gid

```bash
sudo chown www-data:www-data /srv/virtualenvs/oauthclientbridge/run/spotify.db
sudo chown www-data:www-data /srv/virtualenvs/oauthclientbridge/run/soundcloud.db
```

## 3) Create and initialize Prometheus multiproc volumes

```bash
sudo podman volume create oauthclientbridge-spotify-prom
sudo podman volume create oauthclientbridge-soundcloud-prom

sudo podman run --rm -v oauthclientbridge-spotify-prom:/vol docker.io/library/busybox:latest sh -c 'chown -R 33:33 /vol && chmod -R ug+rwX /vol'
sudo podman run --rm -v oauthclientbridge-soundcloud-prom:/vol docker.io/library/busybox:latest sh -c 'chown -R 33:33 /vol && chmod -R ug+rwX /vol'
```

## 4) Install and start Quadlet units

```bash
sudo install -D -m 0644 deploy/spotify/container /etc/containers/systemd/oauthclientbridge-spotify.container
sudo install -D -m 0644 deploy/soundcloud/container /etc/containers/systemd/oauthclientbridge-soundcloud.container

sudo systemctl daemon-reload
sudo systemctl enable --now oauthclientbridge-spotify.service
sudo systemctl enable --now oauthclientbridge-soundcloud.service
```

## 5) Verify

```bash
sudo systemctl status oauthclientbridge-spotify.service
sudo systemctl status oauthclientbridge-soundcloud.service
sudo podman ps --filter name=oauthclientbridge-
sudo podman logs oauthclientbridge-spotify
sudo podman logs oauthclientbridge-soundcloud
```

## Socket paths for Caddy uWSGI upstreams

- Spotify: `/run/oauthclientbridge/spotify/uwsgi.sock`
- SoundCloud: `/run/oauthclientbridge/soundcloud/uwsgi.sock`

Example Caddy snippet (uWSGI transport):

```caddy
auth.mopidy.com {
  redir /spotify /spotify/ 308
  redir /soundcloud /soundcloud/ 308

  handle_path /spotify/* {
    reverse_proxy unix//run/oauthclientbridge/spotify/uwsgi.sock {
      transport uwsgi
    }
  }

  handle_path /soundcloud/* {
    reverse_proxy unix//run/oauthclientbridge/soundcloud/uwsgi.sock {
      transport uwsgi
    }
  }
}
```

`handle_path` strips the prefix before proxying, replacing old uWSGI
`mount=/spotify` / `mount=/soundcloud` behavior.

## Future sops-nix wiring

If moving to sops-nix later, either:

- split secret keys into separate env files and add additional
  `EnvironmentFile=` lines in quadlet, or
- keep single env files and let sops render the whole file.
