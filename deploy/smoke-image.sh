#!/usr/bin/env bash
set -euo pipefail

runtime="${SMOKE_RUNTIME:-asgi}"
case "$runtime" in
  asgi)
    default_image="ghcr.io/adamcik/oauthclientbridge:asgi"
    socket_dir="/run/asgi"
    runtime_args=(--uds "$socket_dir/server.sock")
    ;;
  wsgi)
    default_image="ghcr.io/adamcik/oauthclientbridge:latest"
    socket_dir="/run/uwsgi"
    runtime_args=(--http-socket "$socket_dir/server.sock" --chmod-socket=660 --vacuum)
    ;;
  *)
    echo "SMOKE_RUNTIME must be 'asgi' or 'wsgi'" >&2
    exit 2
    ;;
esac

image="${SMOKE_IMAGE:-$default_image}"
container="oauthclientbridge-${runtime}-smoke-${RANDOM}"

if [ "$(uname -s)" != Linux ]; then
  echo "The image smoke test requires Podman host networking on Linux" >&2
  exit 1
fi

if ! command -v podman >/dev/null || ! command -v newuidmap >/dev/null; then
  echo "The image smoke test requires Podman and the newuidmap helper" >&2
  exit 1
fi

if [ "$(podman info --format '{{.Host.Security.Rootless}}')" != true ]; then
  echo "The image smoke test must run with rootless Podman" >&2
  exit 1
fi

workspace="$(mktemp -d)"
upstream_pid=""

cleanup() {
  status=$?
  if podman container inspect "$container" >/dev/null 2>&1; then
    if [ "$status" -ne 0 ]; then
      podman logs "$container" >&2 || true
    fi
    podman rm --force "$container" >/dev/null 2>&1 || true
  fi
  if [ -n "$upstream_pid" ]; then
    kill "$upstream_pid" >/dev/null 2>&1 || true
    wait "$upstream_pid" 2>/dev/null || true
  fi
  rm -rf "$workspace"
  exit "$status"
}
trap cleanup EXIT

mkdir -m 0777 "$workspace/data" "$workspace/run"
python - "$workspace/data/sqlite.db" <<'PY'
import sqlite3
import sys
from pathlib import Path

database = Path(sys.argv[1])
schema = Path("src/oauthclientbridge/schema.sql").read_text()
with sqlite3.connect(database) as connection:
    connection.executescript(schema)
database.chmod(0o666)
PY

cat >"$workspace/callback.html" <<'EOF'
{"client_id": {{ client_id|tojson|safe }}, "client_secret": {{ client_secret|tojson|safe }}}
EOF
chmod 0644 "$workspace/callback.html"

python tests/container_smoke/fake_upstream.py \
  --port-file "$workspace/upstream-port" &
upstream_pid=$!

for _ in $(seq 1 100); do
  if [ -s "$workspace/upstream-port" ]; then
    break
  fi
  if ! kill -0 "$upstream_pid" 2>/dev/null; then
    echo "Fake OAuth provider exited during startup" >&2
    exit 1
  fi
  sleep 0.05
done
test -s "$workspace/upstream-port"
upstream_port="$(cat "$workspace/upstream-port")"

podman run --detach \
  --name "$container" \
  --network host \
  --read-only \
  --user "$(id -u):$(id -g)" \
  --cap-drop ALL \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777 \
  --tmpfs /run/prom:rw,nosuid,nodev,noexec,size=16m,mode=0777 \
  --mount "type=bind,src=$workspace/data,dst=/data" \
  --mount "type=bind,src=$workspace/run,dst=$socket_dir" \
  --mount "type=bind,src=$workspace/callback.html,dst=/config/callback.html,readonly" \
  --env BRIDGE_SESSION_SECRET=container-smoke-session-secret \
  --env BRIDGE_SESSION_COOKIE_DOMAIN=auth.example.com \
  --env BRIDGE_SESSION_COOKIE_PATH=/spotify \
  --env BRIDGE_SESSION_COOKIE_SECURE=true \
  --env OAUTH_CLIENT_ID=smoke-client \
  --env OAUTH_CLIENT_SECRET=smoke-secret \
  --env "OAUTH_AUTHORIZATION_URI=http://127.0.0.1:$upstream_port/authorize" \
  --env "OAUTH_TOKEN_URI=http://127.0.0.1:$upstream_port/token" \
  --env OAUTH_REDIRECT_URI=https://auth.example.com/spotify/callback \
  --env SENTRY_ENABLED=false \
  --env TELEMETRY_COMPONENTS='[]' \
  --env WORKERS=1 \
  --env THREADS=1 \
  "$image" \
  "${runtime_args[@]}" >/dev/null

for _ in $(seq 1 200); do
  if [ -S "$workspace/run/server.sock" ]; then
    break
  fi
  if ! podman container inspect --format '{{.State.Running}}' "$container" \
    | grep --quiet true; then
    echo "$runtime container exited during startup" >&2
    exit 1
  fi
  sleep 0.05
done
test -S "$workspace/run/server.sock"

python tests/container_smoke/smoke.py --runtime "$runtime" "$workspace/run/server.sock"

timeout 45 podman stop --time 35 "$container" >/dev/null
exit_code="$(podman container inspect --format '{{.State.ExitCode}}' "$container")"
if [ "$exit_code" != 0 ] && [ "$exit_code" != 143 ]; then
  echo "$runtime container exited with status $exit_code" >&2
  exit 1
fi

if [ "$runtime" = asgi ]; then
  if ! podman logs "$container" 2>&1 \
    | grep --fixed-strings --quiet "Application shutdown complete"; then
    echo "ASGI container did not complete graceful application shutdown" >&2
    exit 1
  fi
fi

if podman logs "$container" 2>&1 | grep --extended-regexp \
  'Traceback \(most recent call last\)|Application startup failed|ERROR:'; then
  echo "$runtime container logged an unexpected runtime error" >&2
  exit 1
fi
