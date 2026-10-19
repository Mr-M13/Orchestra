#!/usr/bin/env bash
# Stage or install the PixelBox coordinator.  The default is a read-only plan.
#
# This script never edits the Nextcloud Docker stack, Docker daemon, Tailscale
# Serve configuration, or the existing Mac service.  Applying it is a cutover
# action and therefore requires an explicit owner approval marker.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALL_ROOT="${ORCHESTRA_INSTALL_ROOT:-/opt/orchestra}"
CONFIG_ROOT="${ORCHESTRA_CONFIG_ROOT:-/etc/orchestra}"
STATE_ROOT="${ORCHESTRA_STATE_ROOT:-/home/hamed/orchestra-migration/state}"
SERVICE_USER="${ORCHESTRA_SERVICE_USER:-hamed}"
APPLY=0

usage() {
  cat <<'EOF'
Usage: install.sh [--apply]

Without --apply, print the exact installation plan and verify local inputs.
With --apply, copy the reviewed source tree and systemd units, then enable and
start Orchestra.  --apply also requires:
  ORCHESTRA_CUTOVER_APPROVED=1
EOF
}

for argument in "$@"; do
  case "$argument" in
    --apply) APPLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $argument" >&2; usage >&2; exit 2 ;;
  esac
done

fail() { echo "ERROR: $*" >&2; exit 1; }
note() { echo "  $*"; }

[[ "$(id -un)" == "$SERVICE_USER" || "$APPLY" == "0" ]] \
  || fail "run as $SERVICE_USER (or perform a dry-run first)"

for required in \
  "$SOURCE_ROOT/omcp/server.py" \
  "$SOURCE_ROOT/omcp/health.py" \
  "$SOURCE_ROOT/.github/workflows/orchestra-worker.yml" \
  "$SOURCE_ROOT/deploy/pixelbox/systemd/orchestra-omcp.service"; do
  [[ -e "$required" ]] || fail "missing reviewed source file: $required"
done

echo "Orchestra PixelBox installation plan"
note "source:       $SOURCE_ROOT"
note "install root: $INSTALL_ROOT"
note "config root:  $CONFIG_ROOT"
note "state root:   $STATE_ROOT"
note "service user: $SERVICE_USER"
note "units:        orchestra-omcp.service orchestra-tunnel.service orchestra-health.timer"
note "untouched:    Nextcloud services, Docker, Tailscale Serve, existing Mac Orchestra"
echo

if [[ "$APPLY" == "0" ]]; then
  echo "Dry run complete. No files or services were changed."
  echo "After explicit approval, re-run with --apply and ORCHESTRA_CUTOVER_APPROVED=1."
  exit 0
fi

[[ "${ORCHESTRA_CUTOVER_APPROVED:-0}" == "1" ]] \
  || fail "cutover is not approved; set ORCHESTRA_CUTOVER_APPROVED=1 only after approval"

command -v sudo >/dev/null || fail "sudo is required"
command -v rsync >/dev/null || fail "rsync is required"
command -v systemctl >/dev/null || fail "systemctl is required"

sudo install -d -m 0755 -o root -g root "$INSTALL_ROOT"
sudo install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_ROOT"
sudo install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_ROOT/secrets"
sudo install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_ROOT"

# Copy only the reviewed working tree.  Keep the checkout under the state
# directory separate from this deployment copy.
sudo rsync -a --delete \
  --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
  "$SOURCE_ROOT/" "$INSTALL_ROOT/"

for unit in orchestra-omcp.service orchestra-tunnel.service \
            orchestra-health.service orchestra-health.timer; do
  sudo install -m 0644 "$SOURCE_ROOT/deploy/pixelbox/systemd/$unit" \
    "/etc/systemd/system/$unit"
done

if [[ ! -f "$CONFIG_ROOT/omcp.json" ]]; then
  sudo install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" \
    "$SOURCE_ROOT/deploy/pixelbox/omcp.example.json" "$CONFIG_ROOT/omcp.json"
  echo "Installed example omcp.json. Review paths, workers and profile IDs before starting."
fi
for env_file in omcp.env tunnel.env; do
  if [[ ! -f "$CONFIG_ROOT/$env_file" ]]; then
    sudo install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" \
      "$SOURCE_ROOT/deploy/pixelbox/$env_file.example" "$CONFIG_ROOT/$env_file"
    echo "Installed $CONFIG_ROOT/$env_file. Fill it locally; do not commit secrets."
  fi
done
if [[ ! -f "$CONFIG_ROOT/secrets/github-actions.token" ]]; then
  echo "Create $CONFIG_ROOT/secrets/github-actions.token (mode 0600) before starting."
fi

sudo systemctl daemon-reload
sudo systemctl enable --now orchestra-omcp.service orchestra-health.timer
# The tunnel is enabled separately after its profile and access boundary have
# been verified.  This avoids exposing mutating tools prematurely.
echo "Orchestra coordinator enabled. Tunnel remains disabled until verified:"
echo "  sudo systemctl enable --now orchestra-tunnel.service"
echo "Run deploy/pixelbox/verify.sh and record evidence before routing ChatGPT."
