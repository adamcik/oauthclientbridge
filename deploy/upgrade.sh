#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  deploy/upgrade.sh [options]

Options:
  --image <ref>    Image ref to pull (default: ghcr.io/adamcik/oauthclientbridge:latest)
  --recreate       Recreate containers + regenerate systemd units
  --check          Pull and compare only; do not restart/recreate
  -h, --help       Show this help

Examples:
  deploy/upgrade.sh
  deploy/upgrade.sh --check
  deploy/upgrade.sh --image ghcr.io/adamcik/oauthclientbridge@sha256:abcd...
  deploy/upgrade.sh --recreate
EOF
}

IMAGE_REF="ghcr.io/adamcik/oauthclientbridge:latest"
DO_RECREATE=0
CHECK_ONLY=0

CONTAINERS=(
  oauthclientbridge-spotify
  oauthclientbridge-soundcloud
)

UNITS=(
  container-oauthclientbridge-spotify.service
  container-oauthclientbridge-soundcloud.service
)

while [ "$#" -gt 0 ]; do
  case "$1" in
    --image)
      IMAGE_REF="$2"
      shift 2
      ;;
    --recreate)
      DO_RECREATE=1
      shift
      ;;
    --check)
      CHECK_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

log() {
  printf '\n==> %s\n' "$*"
}

for container in "${CONTAINERS[@]}"; do
  if ! sudo podman container exists "$container"; then
    echo "Container does not exist: $container" >&2
    exit 1
  fi
done

declare -A BEFORE_NAME
declare -A BEFORE_ID

log "Current runtime images"
for container in "${CONTAINERS[@]}"; do
  BEFORE_NAME["$container"]="$(sudo podman inspect "$container" --format '{{.ImageName}}')"
  BEFORE_ID["$container"]="$(sudo podman inspect "$container" --format '{{.Image}}')"
  echo "$container"
  echo "  ImageName: ${BEFORE_NAME[$container]}"
  echo "  ImageID:   ${BEFORE_ID[$container]}"
done

log "Pull image"
sudo podman pull "$IMAGE_REF"

TARGET_ID="$(sudo podman image inspect "$IMAGE_REF" --format '{{.Id}}')"
log "Pulled target image"
echo "TargetRef: $IMAGE_REF"
echo "TargetID:  $TARGET_ID"

if [ "$CHECK_ONLY" -eq 1 ]; then
  all_match=1
  for container in "${CONTAINERS[@]}"; do
    if [ "${BEFORE_ID[$container]}" = "$TARGET_ID" ]; then
      echo "$container: up to date"
    else
      echo "$container: runtime differs from pulled target"
      all_match=0
    fi
  done

  if [ "$all_match" -eq 1 ]; then
    exit 0
  fi

  exit 1
fi

if [ "$DO_RECREATE" -eq 0 ]; then
  log "Restart units (default mode; no recreate)"
  sudo systemctl restart "${UNITS[@]}"
  for unit in "${UNITS[@]}"; do
    sudo systemctl is-active --quiet "$unit"
  done
else
  log "Recreate containers + regenerate units"
  sudo systemctl stop "${UNITS[@]}" || true
  sudo podman rm -f "${CONTAINERS[@]}" || true

  sudo podman create \
    --name oauthclientbridge-spotify \
    --network host \
    --user 33:33 \
    --cap-drop=ALL \
    --security-opt no-new-privileges \
    --read-only \
    --env-file /etc/oauthclientbridge/spotify/env \
    -v /var/lib/oauthclientbridge/spotify:/data:rw,nosuid,nodev,noexec \
    -v /run/oauthclientbridge/spotify:/run/uwsgi:rw,nosuid,nodev,noexec \
    -v /etc/oauthclientbridge/spotify/callback.html:/config/callback.html:ro \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777 \
    --tmpfs /run/prom:rw,nosuid,nodev,noexec,size=64m,mode=1777 \
    "$IMAGE_REF" \
    --socket /run/uwsgi/uwsgi.sock --chmod-socket=660 --vacuum

  sudo podman create \
    --name oauthclientbridge-soundcloud \
    --network host \
    --user 33:33 \
    --cap-drop=ALL \
    --security-opt no-new-privileges \
    --read-only \
    --env-file /etc/oauthclientbridge/soundcloud/env \
    -v /var/lib/oauthclientbridge/soundcloud:/data:rw,nosuid,nodev,noexec \
    -v /run/oauthclientbridge/soundcloud:/run/uwsgi:rw,nosuid,nodev,noexec \
    -v /etc/oauthclientbridge/soundcloud/callback.html:/config/callback.html:ro \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777 \
    --tmpfs /run/prom:rw,nosuid,nodev,noexec,size=64m,mode=1777 \
    "$IMAGE_REF" \
    --socket /run/uwsgi/uwsgi.sock --chmod-socket=660 --vacuum

  sudo podman generate systemd --name oauthclientbridge-spotify --files --new
  sudo podman generate systemd --name oauthclientbridge-soundcloud --files --new

  sudo install -D -m 0644 container-oauthclientbridge-spotify.service /etc/systemd/system/container-oauthclientbridge-spotify.service
  sudo install -D -m 0644 container-oauthclientbridge-soundcloud.service /etc/systemd/system/container-oauthclientbridge-soundcloud.service

  sudo systemctl daemon-reload
  sudo systemctl enable --now "${UNITS[@]}"
fi

log "Runtime images after action"
all_match=1
for container in "${CONTAINERS[@]}"; do
  after_name="$(sudo podman inspect "$container" --format '{{.ImageName}}')"
  after_id="$(sudo podman inspect "$container" --format '{{.Image}}')"
  echo "$container"
  echo "  ImageName: $after_name"
  echo "  ImageID:   $after_id"

  if [ "$after_id" = "$TARGET_ID" ]; then
    echo "  Result: runtime matches target"
  else
    echo "  Result: runtime does not match target" >&2
    all_match=0
  fi
done

if [ "$all_match" -ne 1 ]; then
  if [ "$DO_RECREATE" -eq 0 ]; then
    echo "Hint: retry with --recreate" >&2
  fi
  exit 1
fi

log "Recent logs"
sudo podman logs --tail 50 oauthclientbridge-spotify || true
sudo podman logs --tail 50 oauthclientbridge-soundcloud || true
