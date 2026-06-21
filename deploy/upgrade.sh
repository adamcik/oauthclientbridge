#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<EOF
Usage:
  $0 --instance <name> [options]

Options:
  --instance <name>    Instance name (examples: spotify, soundcloud, spotify-prod)
  --image <ref>        Image ref to pull + set in quadlet
                      (default: ghcr.io/adamcik/oauthclientbridge:latest)
  --unit <name>        systemd unit (default: oauthclientbridge-<instance>.service)
  --container <name>   container name (default: oauthclientbridge-<instance>)
  --quadlet-file <p>   Quadlet path
                      (default: /etc/containers/systemd/oauthclientbridge-<instance>.container)
  --image-override <p> Quadlet drop-in path for Image= override
                      (default: <quadlet-file>.d/image.conf)
  --check              Pull and compare only; do not modify quadlet or restart
  --dry-run            Print resolved configuration and exit
  -h, --help           Show this help

Examples:
  $0 --instance spotify
  $0 --instance spotify --check
  $0 --instance spotify-prod --image ghcr.io/adamcik/oauthclientbridge@sha256:abcd...
EOF
}

INSTANCE_NAME=""
IMAGE_REF="ghcr.io/adamcik/oauthclientbridge:latest"

UNIT_NAME=""
CONTAINER_NAME=""
QUADLET_FILE=""
IMAGE_OVERRIDE_FILE=""

CHECK_ONLY=0
DRY_RUN=0

UNIT_SET=0
CONTAINER_SET=0
QUADLET_SET=0
IMAGE_OVERRIDE_SET=0

TARGET_DIGEST_REF=""
TARGET_OVERRIDE_REF=""

log() {
  printf '\n==> %s\n' "$*"
}

unit_to_quadlet_path() {
  local unit="$1"
  local base
  base="${unit%.service}"
  printf '/etc/containers/systemd/%s.container\n' "$base"
}

apply_instance_defaults() {
  if [ -z "$INSTANCE_NAME" ]; then
    echo "Missing required argument: --instance" >&2
    usage
    exit 2
  fi

  if [ "$CONTAINER_SET" -eq 0 ]; then
    CONTAINER_NAME="oauthclientbridge-${INSTANCE_NAME}"
  fi
  if [ "$UNIT_SET" -eq 0 ]; then
    UNIT_NAME="oauthclientbridge-${INSTANCE_NAME}.service"
  fi
  if [ "$QUADLET_SET" -eq 0 ]; then
    QUADLET_FILE="$(unit_to_quadlet_path "$UNIT_NAME")"
  fi
  if [ "$IMAGE_OVERRIDE_SET" -eq 0 ]; then
    IMAGE_OVERRIDE_FILE="${QUADLET_FILE}.d/image.conf"
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --instance)
      INSTANCE_NAME="$2"
      shift 2
      ;;
    --image)
      IMAGE_REF="$2"
      shift 2
      ;;
    --unit)
      UNIT_NAME="$2"
      UNIT_SET=1
      shift 2
      ;;
    --container)
      CONTAINER_NAME="$2"
      CONTAINER_SET=1
      shift 2
      ;;
    --quadlet-file)
      QUADLET_FILE="$2"
      QUADLET_SET=1
      shift 2
      ;;
    --image-override)
      IMAGE_OVERRIDE_FILE="$2"
      IMAGE_OVERRIDE_SET=1
      shift 2
      ;;
    --check)
      CHECK_ONLY=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
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

apply_instance_defaults

log "Resolved configuration"
echo "Instance:    $INSTANCE_NAME"
echo "Container:   $CONTAINER_NAME"
echo "Unit:        $UNIT_NAME"
echo "QuadletFile: $QUADLET_FILE"
echo "ImageOvr:    $IMAGE_OVERRIDE_FILE"
echo "ImageRef:    $IMAGE_REF"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "Dry run: no changes applied"
  exit 0
fi

if ! sudo test -f "$QUADLET_FILE"; then
  echo "Quadlet file not found: $QUADLET_FILE" >&2
  exit 1
fi

log "Current runtime image"
if sudo podman container exists "$CONTAINER_NAME"; then
  before_id="$(sudo podman inspect "$CONTAINER_NAME" --format '{{.Image}}')"
  before_name="$(sudo podman inspect "$CONTAINER_NAME" --format '{{.ImageName}}')"
  before_repo_digest="$(sudo podman image inspect "$before_name" --format '{{index .RepoDigests 0}}' 2>/dev/null || true)"
  echo "Container: $CONTAINER_NAME"
  echo "ImageName:  $before_name"
  echo "ImageID:    $before_id"
  echo "RepoDigest: ${before_repo_digest:-<none>}"
else
  echo "Container not found: $CONTAINER_NAME"
  before_id=""
  before_name=""
  before_repo_digest=""
fi

log "Pull image"
sudo podman pull "$IMAGE_REF"
target_id="$(sudo podman image inspect "$IMAGE_REF" --format '{{.Id}}')"
TARGET_DIGEST_REF="$(sudo podman image inspect "$IMAGE_REF" --format '{{index .RepoDigests 0}}' 2>/dev/null || true)"
if [ -n "$TARGET_DIGEST_REF" ]; then
  TARGET_OVERRIDE_REF="$TARGET_DIGEST_REF"
else
  TARGET_OVERRIDE_REF="$IMAGE_REF"
fi
echo "TargetRef: $IMAGE_REF"
echo "TargetID:  $target_id"
echo "TargetPin: $TARGET_OVERRIDE_REF"

if [ "$CHECK_ONLY" -eq 1 ]; then
  if [ -n "$before_id" ] && [ "$before_id" = "$target_id" ]; then
    echo "Status: up to date"
    exit 0
  fi
  echo "Status: runtime differs from pulled target"
  exit 1
fi

log "Write quadlet Image= drop-in override"
sudo install -d -m 0755 "$(dirname "$IMAGE_OVERRIDE_FILE")"
sudo tee "$IMAGE_OVERRIDE_FILE" >/dev/null <<EOF
# Managed by deploy/upgrade.sh
# Requested image: $IMAGE_REF
# To roll back, set Image=<previous-ref> and restart $UNIT_NAME.
# Previous runtime image: ${before_repo_digest:-${before_name:-<unknown>}}
[Container]
Image=$TARGET_OVERRIDE_REF # requested: $IMAGE_REF
EOF

log "Reload + restart service"
sudo systemctl daemon-reload
sudo systemctl restart "$UNIT_NAME"
sudo systemctl is-active --quiet "$UNIT_NAME"

after_id="$(sudo podman inspect "$CONTAINER_NAME" --format '{{.Image}}')"
after_name="$(sudo podman inspect "$CONTAINER_NAME" --format '{{.ImageName}}')"
log "Runtime image after action"
echo "ImageName: $after_name"
echo "ImageID:   $after_id"

if [ "$after_id" = "$target_id" ]; then
  echo "Result: runtime matches target"
else
  echo "Result: runtime does not match target" >&2
  echo "Hint: verify $QUADLET_FILE and unit $UNIT_NAME" >&2
  exit 1
fi

log "Recent logs"
sudo podman logs --tail 50 "$CONTAINER_NAME" || true
