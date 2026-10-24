#!/usr/bin/env bash
# Stage and activate an exact, reviewed PixelBox release. The default is a
# read-only plan. No mode manages the tunnel or unrelated host services.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALL_ROOT="${ORCHESTRA_INSTALL_ROOT:-/opt/orchestra}"
CONFIG_ROOT="${ORCHESTRA_CONFIG_ROOT:-/etc/orchestra}"
STATE_ROOT="${ORCHESTRA_STATE_ROOT:-/home/hamed/orchestra-migration/state}"
SERVICE_USER="${ORCHESTRA_SERVICE_USER:-hamed}"
RELEASES_ROOT="$INSTALL_ROOT/releases"
CURRENT_LINK="$INSTALL_ROOT/current"
PREVIOUS_LINK="$INSTALL_ROOT/previous"
MODE=plan
RELEASE_SHA="${ORCHESTRA_RELEASE_SHA:-}"
STAGED_ONLY=0
ARCHIVE_FILE=""
STAGE_ROOT=""

usage() {
  cat <<'EOF'
Usage: install.sh [--stage|--activate|--rollback|--apply] --sha <40-hex-sha>

Without a mode, print the exact release plan and change nothing.
  --stage     export and validate the exact Git commit into releases/<sha>
  --activate  atomically switch current to an already staged release
  --rollback  atomically switch current to an older staged release
  --apply     stage, then activate (kept for the original two-pass workflow)

Staging requires ORCHESTRA_DEPLOY_APPROVED=1. Activation, rollback, and apply
also require ORCHESTRA_CUTOVER_APPROVED=1. Activation is supported only on the
reviewed Linux ARM64 PixelBox paths and never starts the tunnel.
EOF
}

fail() { echo "ERROR: $*" >&2; exit 1; }
note() { echo "  $*"; }

set_mode() {
  [[ "$MODE" == "plan" ]] || fail "choose exactly one deployment mode"
  MODE="$1"
}

while (($#)); do
  case "$1" in
    --stage) set_mode stage ;;
    --activate) set_mode activate ;;
    --rollback) set_mode rollback ;;
    --apply) set_mode apply ;;
    --sha)
      shift
      (($#)) || fail "--sha requires a value"
      RELEASE_SHA="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

SOURCE_HEAD="unavailable"
case "$MODE" in
  plan|stage|apply)
    command -v git >/dev/null || fail "git is required"
    SOURCE_HEAD="$(git -C "$SOURCE_ROOT" rev-parse --verify HEAD^{commit} 2>/dev/null)" \
      || fail "source is not a Git checkout"
    if [[ -z "$RELEASE_SHA" ]]; then
      [[ "$MODE" == "plan" ]] || fail "--sha must name the reviewed 40-character commit"
      RELEASE_SHA="$SOURCE_HEAD"
    fi
    ;;
  activate|rollback)
    [[ -n "$RELEASE_SHA" ]] || fail "--sha must name the staged 40-character commit"
    ;;
esac
[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] \
  || fail "release SHA must be exactly 40 lowercase hexadecimal characters"

validate_source_commit() {
  local resolved required
  resolved="$(git -C "$SOURCE_ROOT" rev-parse --verify "$RELEASE_SHA^{commit}" 2>/dev/null)" \
    || fail "release commit is not present in the source repository: $RELEASE_SHA"
  [[ "$resolved" == "$RELEASE_SHA" ]] || fail "release SHA did not resolve exactly"
  for required in \
    omcp/server.py \
    omcp/health.py \
    .github/workflows/orchestra-worker.yml \
    deploy/pixelbox/systemd/orchestra-omcp.service \
    deploy/pixelbox/systemd/orchestra-github-dispatch.service; do
    git -C "$SOURCE_ROOT" cat-file -e "$RELEASE_SHA:$required" 2>/dev/null \
      || fail "release commit is missing reviewed file: $required"
  done
}

if [[ "$MODE" == "plan" || "$MODE" == "stage" || "$MODE" == "apply" ]]; then
  validate_source_commit
fi

echo "Orchestra PixelBox release plan"
note "source repository: $SOURCE_ROOT"
note "source HEAD:       $SOURCE_HEAD"
note "exact release:     $RELEASE_SHA"
note "release directory: $RELEASES_ROOT/$RELEASE_SHA"
note "active symlink:    $CURRENT_LINK"
note "config root:       $CONFIG_ROOT"
note "state root:        $STATE_ROOT"
note "service user:      $SERVICE_USER"
note "mode:              $MODE"
note "untouched:         tunnel, Nextcloud, Docker, Tailscale, Mac coordinator"
echo

if [[ "$MODE" == "plan" ]]; then
  echo "Dry run complete. No files or services were changed; no symlinks were changed."
  echo "Stage this exact commit with:"
  echo "  ORCHESTRA_DEPLOY_APPROVED=1 $0 --stage --sha $RELEASE_SHA"
  exit 0
fi

[[ "${ORCHESTRA_DEPLOY_APPROVED:-0}" == "1" ]] \
  || fail "deployment staging is not approved; set ORCHESTRA_DEPLOY_APPROVED=1 only after approval"
[[ "$(id -un)" == "$SERVICE_USER" ]] || fail "run as $SERVICE_USER"
command -v sudo >/dev/null || fail "sudo is required"
command -v tar >/dev/null || fail "tar is required"
command -v python3 >/dev/null || fail "python3 is required"

cleanup() {
  if [[ -n "$ARCHIVE_FILE" && -f "$ARCHIVE_FILE" ]]; then
    rm -f -- "$ARCHIVE_FILE"
  fi
  if [[ -n "$STAGE_ROOT" ]]; then
    case "$STAGE_ROOT" in
      "$RELEASES_ROOT"/.staging-"$RELEASE_SHA".*)
        sudo rm -rf -- "$STAGE_ROOT"
        ;;
      *) echo "WARNING: refused to clean unexpected staging path: $STAGE_ROOT" >&2 ;;
    esac
  fi
}
trap cleanup EXIT

calculate_release_digest() {
  python3 - "$1" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
digest = hashlib.sha256()
excluded = {".orchestra-release-sha", ".orchestra-release-digest"}
for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
    relative = path.relative_to(root).as_posix()
    if relative in excluded:
        continue
    metadata = path.lstat()
    if stat.S_ISDIR(metadata.st_mode):
        continue
    digest.update(relative.encode("utf-8") + b"\0")
    if stat.S_ISLNK(metadata.st_mode):
        digest.update(b"L\0" + os.readlink(path).encode("utf-8") + b"\0")
    elif stat.S_ISREG(metadata.st_mode):
        digest.update(b"X\0" if metadata.st_mode & 0o111 else b"F\0")
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    else:
        raise SystemExit(f"unsupported release entry: {relative}")
print(digest.hexdigest())
PY
}

validate_release_tree() {
  local release_root="$1" marker recorded_digest actual_digest cache_root
  marker="$(sudo cat "$release_root/.orchestra-release-sha" 2>/dev/null)" \
    || fail "release marker is missing: $release_root"
  [[ "$marker" == "$RELEASE_SHA" ]] \
    || fail "release marker does not match requested SHA: $release_root"
  for required in \
    omcp/server.py \
    omcp/health.py \
    .github/workflows/orchestra-worker.yml \
    deploy/pixelbox/install.sh \
    deploy/pixelbox/verify.sh \
    deploy/pixelbox/systemd/orchestra-omcp.service \
    deploy/pixelbox/systemd/orchestra-github-dispatch.service \
    deploy/pixelbox/github-dispatch.example.json; do
    sudo test -r "$release_root/$required" \
      || fail "staged release is missing reviewed file: $required"
  done
  recorded_digest="$(sudo cat "$release_root/.orchestra-release-digest" 2>/dev/null)" \
    || fail "release digest is missing: $release_root"
  [[ "$recorded_digest" =~ ^[0-9a-f]{64}$ ]] || fail "release digest is invalid"
  actual_digest="$(calculate_release_digest "$release_root")" \
    || fail "could not calculate staged release digest"
  [[ "$actual_digest" == "$recorded_digest" ]] \
    || fail "staged release content differs from its validated digest"
  bash -n "$release_root/deploy/pixelbox/install.sh"
  bash -n "$release_root/deploy/pixelbox/verify.sh"
  cache_root="$(mktemp -d "${TMPDIR:-/tmp}/orchestra-pycache.XXXXXX")"
  if ! (cd "$release_root" && PYTHONPYCACHEPREFIX="$cache_root" \
      python3 -m compileall -q omcp scripts tests); then
    rm -rf -- "$cache_root"
    fail "Python compilation failed for staged release"
  fi
  rm -rf -- "$cache_root"
}

stage_release() {
  local release_root="$RELEASES_ROOT/$RELEASE_SHA"
  sudo install -d -m 0755 -o root -g root "$INSTALL_ROOT" "$RELEASES_ROOT"
  if sudo test -e "$release_root"; then
    validate_release_tree "$release_root"
    echo "Exact release is already staged and valid: $release_root"
    return
  fi

  ARCHIVE_FILE="$(umask 077 && mktemp "${TMPDIR:-/tmp}/orchestra-release.XXXXXX.tar")"
  git -C "$SOURCE_ROOT" archive --format=tar --output="$ARCHIVE_FILE" "$RELEASE_SHA"
  STAGE_ROOT="$RELEASES_ROOT/.staging-$RELEASE_SHA.$$"
  sudo install -d -m 0755 -o root -g root "$STAGE_ROOT"
  sudo tar -xf "$ARCHIVE_FILE" -C "$STAGE_ROOT"
  printf '%s\n' "$RELEASE_SHA" | sudo tee "$STAGE_ROOT/.orchestra-release-sha" >/dev/null
  calculate_release_digest "$STAGE_ROOT" \
    | sudo tee "$STAGE_ROOT/.orchestra-release-digest" >/dev/null
  sudo chmod 0444 "$STAGE_ROOT/.orchestra-release-sha"
  sudo chmod 0444 "$STAGE_ROOT/.orchestra-release-digest"
  validate_release_tree "$STAGE_ROOT"
  sudo chmod -R go-w "$STAGE_ROOT"
  sudo mv -- "$STAGE_ROOT" "$release_root"
  STAGE_ROOT=""
  echo "Staged and validated exact release: $release_root"
}

stage_configuration() {
  local release_root="$RELEASES_ROOT/$RELEASE_SHA" env_file
  sudo install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_ROOT"
  sudo install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_ROOT/secrets"
  sudo install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_ROOT"
  sudo install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" \
    "$(dirname "$STATE_ROOT")/backup"

  if [[ ! -f "$CONFIG_ROOT/omcp.json" ]]; then
    sudo install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" \
      "$release_root/deploy/pixelbox/omcp.example.json" "$CONFIG_ROOT/omcp.json"
    echo "Installed example omcp.json; owner review is required before activation."
    STAGED_ONLY=1
  fi
  for env_file in omcp.env tunnel.env mcp-http.env; do
    if [[ ! -f "$CONFIG_ROOT/$env_file" ]]; then
      sudo install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" \
        "$release_root/deploy/pixelbox/$env_file.example" "$CONFIG_ROOT/$env_file"
      echo "Installed $CONFIG_ROOT/$env_file; fill it locally and do not commit secrets."
      STAGED_ONLY=1
    fi
  done
  if [[ ! -f "$CONFIG_ROOT/github-dispatch/config.json" \
      || ! -f "$CONFIG_ROOT/github-dispatch/github-actions.token" ]]; then
    echo "Dedicated GitHub broker identity/configuration is not installed. Follow the reviewed migration runbook after separate approval."
    STAGED_ONLY=1
  fi
}

configuration_is_ready_for_activation() {
  if [[ "$STAGED_ONLY" == "1" ]]; then
    echo "Release was staged, but services were not changed. Complete configuration, then use --activate."
    return 3
  fi
}

atomic_symlink() {
  local target="$1" link="$2" temporary="$2.new.$$"
  sudo ln -s -- "$target" "$temporary"
  sudo mv -Tf -- "$temporary" "$link"
}

release_sha_from_link() {
  local link="$1" target
  [[ -L "$link" ]] || return 1
  target="$(readlink "$link")"
  [[ "$target" =~ ^releases/([0-9a-f]{40})$ ]] || return 1
  printf '%s\n' "${BASH_REMATCH[1]}"
}

install_units() {
  local release_root="$1" unit
  for unit in orchestra-omcp.service orchestra-github-dispatch.service orchestra-tunnel.service orchestra-mcp-http.service \
              orchestra-health.service orchestra-health.timer; do
    sudo install -m 0644 "$release_root/deploy/pixelbox/systemd/$unit" \
      "/etc/systemd/system/$unit"
  done
}

preflight_activation() {
  local release_root="$RELEASES_ROOT/$RELEASE_SHA" mode coordinator_uid broker_uid
  [[ "$(uname -s)" == "Linux" ]] || fail "activation is supported only on Linux"
  [[ "$(uname -m)" == "aarch64" || "$(uname -m)" == "arm64" ]] \
    || fail "--apply requires a Linux ARM64 host"
  [[ "$INSTALL_ROOT" == "/opt/orchestra" ]] \
    || fail "activation requires ORCHESTRA_INSTALL_ROOT=/opt/orchestra (units use this path)"
  [[ "$CONFIG_ROOT" == "/etc/orchestra" ]] \
    || fail "activation requires ORCHESTRA_CONFIG_ROOT=/etc/orchestra (units use this path)"
  [[ "$STATE_ROOT" == "/home/hamed/orchestra-migration/state" ]] \
    || fail "activation requires the reviewed PixelBox state path"
  [[ "$SERVICE_USER" == "hamed" ]] \
    || fail "activation requires the reviewed PixelBox service user hamed"
  command -v systemctl >/dev/null || fail "systemctl is required"
  [[ -x /usr/bin/python3 ]] || fail "/usr/bin/python3 is required by the systemd units"
  validate_release_tree "$release_root"
  for file in omcp.json omcp.env tunnel.env; do
    [[ -f "$CONFIG_ROOT/$file" ]] || fail "required owner configuration is missing: $CONFIG_ROOT/$file"
    mode="$(sudo stat -c '%a' "$CONFIG_ROOT/$file")"
    [[ "$mode" == "600" ]] || fail "$CONFIG_ROOT/$file must have mode 0600"
  done
  id orchestra-github >/dev/null 2>&1 || fail "dedicated orchestra-github account is missing"
  getent group orchestra-ipc >/dev/null 2>&1 || fail "orchestra-ipc group is missing"
  coordinator_uid="$(id -u "$SERVICE_USER")"
  broker_uid="$(id -u orchestra-github)"
  [[ "$coordinator_uid" != "$broker_uid" ]] || fail "GitHub broker must use a different UID"
  for file in github-dispatch/config.json github-dispatch/github-actions.token; do
    sudo test -f "$CONFIG_ROOT/$file" || fail "broker configuration is missing: $CONFIG_ROOT/$file"
    [[ "$(sudo stat -c '%a' "$CONFIG_ROOT/$file")" == "600" ]] \
      || fail "$CONFIG_ROOT/$file must have mode 0600"
    [[ "$(sudo stat -c '%u' "$CONFIG_ROOT/$file")" == "$broker_uid" ]] \
      || fail "$CONFIG_ROOT/$file must be owned by orchestra-github"
  done
  [[ "$(sudo stat -c '%a' "$CONFIG_ROOT/github-dispatch")" == "700" ]] \
    || fail "$CONFIG_ROOT/github-dispatch must have mode 0700"
  [[ "$(sudo stat -c '%u' "$CONFIG_ROOT/github-dispatch")" == "$broker_uid" ]] \
    || fail "$CONFIG_ROOT/github-dispatch must be owned by orchestra-github"
  sudo -u orchestra-github PYTHONPATH="$release_root" /usr/bin/python3 -c \
    'import sys; from pathlib import Path; from omcp.github_dispatch_service import ServiceConfig; ServiceConfig.load(Path(sys.argv[1]))' \
    "$CONFIG_ROOT/github-dispatch/config.json" \
    || fail "dispatch broker configuration preflight failed"
}

activate_release() {
  local release_root="$RELEASES_ROOT/$RELEASE_SHA" old_sha="" old_root=""
  old_sha="$(release_sha_from_link "$CURRENT_LINK" || true)"
  if [[ -n "$old_sha" ]]; then
    old_root="$RELEASES_ROOT/$old_sha"
  elif [[ -e "$CURRENT_LINK" ]]; then
    fail "$CURRENT_LINK exists but is not a managed release symlink"
  fi

  install_units "$release_root"
  sudo systemctl daemon-reload
  atomic_symlink "releases/$RELEASE_SHA" "$CURRENT_LINK"

  if sudo systemctl enable --now orchestra-github-dispatch.service \
      && sudo systemctl is-active --quiet orchestra-github-dispatch.service \
      && PYTHONPATH="$release_root" /usr/bin/python3 -c \
        'import sys; from pathlib import Path; from omcp.controller import Config; Config.load(Path(sys.argv[1]))' \
        "$CONFIG_ROOT/omcp.json" \
      && sudo systemctl enable orchestra-omcp.service \
      && sudo systemctl enable --now orchestra-health.timer \
      && sudo systemctl restart orchestra-omcp.service \
      && sudo systemctl is-active --quiet orchestra-omcp.service \
      && sudo systemctl is-active --quiet orchestra-health.timer; then
    if [[ -n "$old_sha" && "$old_sha" != "$RELEASE_SHA" ]]; then
      atomic_symlink "releases/$old_sha" "$PREVIOUS_LINK"
    fi
    echo "Activated exact release $RELEASE_SHA."
    echo "Tunnel remains unchanged and must be enabled only after separate verification."
    return
  fi

  echo "Activation failed; restoring the prior release." >&2
  if [[ -n "$old_sha" && -d "$old_root" ]]; then
    atomic_symlink "releases/$old_sha" "$CURRENT_LINK"
    install_units "$old_root"
    sudo systemctl daemon-reload
    sudo systemctl restart orchestra-github-dispatch.service || true
    sudo systemctl restart orchestra-omcp.service || true
    sudo systemctl start orchestra-health.timer || true
    fail "activation failed; current was restored to $old_sha"
  fi
  sudo unlink "$CURRENT_LINK" 2>/dev/null || true
  sudo systemctl stop orchestra-omcp.service orchestra-github-dispatch.service orchestra-health.timer || true
  sudo systemctl disable orchestra-omcp.service orchestra-github-dispatch.service orchestra-health.timer || true
  fail "first activation failed; no prior managed release was available"
}

case "$MODE" in
  stage)
    stage_release
    stage_configuration
    if [[ "$STAGED_ONLY" == "1" ]]; then
      echo "Release staging is complete; owner-only configuration is not yet activation-ready."
    fi
    ;;
  activate|rollback)
    [[ "${ORCHESTRA_CUTOVER_APPROVED:-0}" == "1" ]] \
      || fail "$MODE is not approved; set ORCHESTRA_CUTOVER_APPROVED=1 only after approval"
    preflight_activation
    activate_release
    ;;
  apply)
    [[ "${ORCHESTRA_CUTOVER_APPROVED:-0}" == "1" ]] \
      || fail "cutover is not approved; set ORCHESTRA_CUTOVER_APPROVED=1 only after approval"
    stage_release
    stage_configuration
    configuration_is_ready_for_activation || exit $?
    preflight_activation
    activate_release
    ;;
esac
