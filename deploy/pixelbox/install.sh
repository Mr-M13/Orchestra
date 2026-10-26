#!/usr/bin/env bash
# Stage and activate an exact, reviewed PixelBox release. The default is a
# read-only plan. No mode manages the tunnel or unrelated host services.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config-access.sh
source "$DEPLOY_ROOT/config-access.sh"
BROKER_USER="${ORCHESTRA_BROKER_USER:-orchestra-github}"
MCP_HTTP_USER="${ORCHESTRA_MCP_HTTP_USER:-orchestra-mcp-http}"
MCP_HTTP_GROUP="${ORCHESTRA_MCP_HTTP_GROUP:-orchestra-mcp}"
INSTALL_ROOT="${ORCHESTRA_INSTALL_ROOT:-/opt/orchestra}"
CONFIG_ROOT="${ORCHESTRA_CONFIG_ROOT:-/etc/orchestra}"
STATE_ROOT="${ORCHESTRA_STATE_ROOT:-/home/hamed/orchestra-migration/state}"
SERVICE_USER="${ORCHESTRA_SERVICE_USER:-hamed}"
UNIT_ROOT="${ORCHESTRA_SYSTEMD_UNIT_ROOT:-/etc/systemd/system}"
VERIFIER_ROOT="${ORCHESTRA_VERIFIER_ROOT:-/usr/local/lib/orchestra}"
VERIFIER_PATH="${ORCHESTRA_VERIFIER_PATH:-$VERIFIER_ROOT/http_gateway_verify.py}"
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

validate_source_commit() {
  local resolved required
  resolved="$(git -C "$SOURCE_ROOT" rev-parse --verify "$RELEASE_SHA^{commit}" 2>/dev/null)" \
    || fail "release commit is not present in the source repository: $RELEASE_SHA"
  [[ "$resolved" == "$RELEASE_SHA" ]] || fail "release SHA did not resolve exactly"
  for required in \
    omcp/server.py \
    omcp/health.py \
    deploy/pixelbox/systemd/orchestra-omcp.service; do
    git -C "$SOURCE_ROOT" cat-file -e "$RELEASE_SHA:$required" 2>/dev/null \
      || fail "release commit is missing reviewed file: $required"
  done
  if git -C "$SOURCE_ROOT" cat-file -e \
      "$RELEASE_SHA:deploy/pixelbox/systemd/orchestra-mcp-http.service" 2>/dev/null; then
    git -C "$SOURCE_ROOT" cat-file -e \
      "$RELEASE_SHA:omcp/http_gateway.py" 2>/dev/null \
      || fail "release commit is missing reviewed HTTPS MCP runtime"
  fi
}

release_has_gateway_unit() {
  [[ -f "$1/deploy/pixelbox/systemd/orchestra-mcp-http.service" ]]
}

gateway_unit_is_hardened() {
  local unit="$1"
  grep -qx 'User=orchestra-mcp-http' "$unit" 2>/dev/null \
    && grep -qx 'Group=orchestra-mcp' "$unit" 2>/dev/null \
    && grep -qx 'WorkingDirectory=/opt/orchestra/current' "$unit" 2>/dev/null \
    && grep -q -- '--socket-owner hamed --socket-group orchestra-mcp' "$unit" 2>/dev/null \
    && grep -qx 'RestrictAddressFamilies=AF_UNIX AF_INET' "$unit" 2>/dev/null \
    && grep -qx 'IPAddressDeny=any' "$unit" 2>/dev/null \
    && grep -qx 'PartOf=orchestra-omcp.service' "$unit" 2>/dev/null
}

gateway_unit_source() {
  local release_root="$1"
  local target="$release_root/deploy/pixelbox/systemd/orchestra-mcp-http.service"
  local stable="$SOURCE_ROOT/deploy/pixelbox/systemd/orchestra-mcp-http.service"
  if ! gateway_unit_is_hardened "$target" && [[ -f "$stable" ]]; then
    printf '%s\n' "$stable"
  else
    printf '%s\n' "$target"
  fi
}

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
    deploy/pixelbox/reproduce-worker-startup.sh \
    deploy/pixelbox/systemd/orchestra-omcp.service; do
    sudo test -r "$release_root/$required" \
      || fail "staged release is missing reviewed file: $required"
  done
  if release_has_gateway_unit "$release_root"; then
    sudo test -r "$release_root/omcp/http_gateway.py" \
      || fail "staged release is missing the reviewed HTTPS MCP runtime"
  fi
  recorded_digest="$(sudo cat "$release_root/.orchestra-release-digest" 2>/dev/null)" \
    || fail "release digest is missing: $release_root"
  [[ "$recorded_digest" =~ ^[0-9a-f]{64}$ ]] || fail "release digest is invalid"
  actual_digest="$(calculate_release_digest "$release_root")" \
    || fail "could not calculate staged release digest"
  [[ "$actual_digest" == "$recorded_digest" ]] \
    || fail "staged release content differs from its validated digest"
  bash -n "$release_root/deploy/pixelbox/install.sh"
  bash -n "$release_root/deploy/pixelbox/verify.sh"
  bash -n "$release_root/deploy/pixelbox/reproduce-worker-startup.sh"
  if [[ -r "$release_root/deploy/pixelbox/verify_mcp_http.sh" ]]; then
    bash -n "$release_root/deploy/pixelbox/verify_mcp_http.sh"
  fi
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

install_environment_file() {
  local release_root="$1" env_file="$2" owner="$3" group="$4"
  local destination="$CONFIG_ROOT/$env_file"
  if [[ -e "$destination" || -L "$destination" ]]; then
    [[ -f "$destination" && ! -L "$destination" ]] \
      || fail "$destination must be a regular owner-managed file, not a symlink"
    # Preserve the existing value. Only repair access for the configured
    # service identity; never print or replace credential contents.
    sudo chown "$owner:$group" "$destination"
    sudo chmod 0600 "$destination"
    return
  fi
  sudo install -m 0600 -o "$owner" -g "$group" \
    "$release_root/deploy/pixelbox/$env_file.example" "$destination"
  echo "Installed $destination; fill it locally and do not commit secrets."
  STAGED_ONLY=1
}

install_gateway_verifier() {
  local candidate="$RELEASES_ROOT/$RELEASE_SHA/omcp/http_gateway_verify.py"
  if [[ ! -s "$candidate" ]]; then
    candidate="$SOURCE_ROOT/omcp/http_gateway_verify.py"
  fi
  [[ -f "$candidate" ]] || return 0
  [[ -s "$candidate" ]] || fail "the HTTPS MCP verifier source is empty"
  command -v sha256sum >/dev/null || fail "sha256sum is required for the stable verifier"
  sudo install -d -m 0755 -o root -g root "$VERIFIER_ROOT"
  sudo install -m 0644 -o root -g root "$candidate" "$VERIFIER_PATH"
  sha256sum "$VERIFIER_PATH" | awk '{print $1}' \
    | sudo tee "$VERIFIER_PATH.sha256" >/dev/null
  sudo chmod 0644 "$VERIFIER_PATH.sha256"
}

stage_configuration() {
  local release_root="$RELEASES_ROOT/$RELEASE_SHA" env_file
  if release_has_gateway_unit "$release_root" && [[ "$(uname -s)" != "Linux" ]]; then
    fail "staging an HTTPS gateway release requires Linux account and ACL tools"
  fi
  sudo install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_ROOT"
  sudo install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_ROOT/secrets"
  sudo install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_ROOT"
  sudo install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" \
    "$(dirname "$STATE_ROOT")/backup"
  [[ "$(sudo stat -c '%u' "$CONFIG_ROOT")" == "$(id -u "$SERVICE_USER")" \
     && "$(sudo stat -c '%g' "$CONFIG_ROOT")" == "$(id -g "$SERVICE_USER")" ]] \
    || fail "$CONFIG_ROOT must be owned by $SERVICE_USER"

  # install -d rewrites the ACL mask on an existing CONFIG_ROOT even when its
  # mode is already 0700.  Re-apply traverse-only entries after the directory
  # operations so repeated staging cannot strand the broker or the HTTP gateway.
  ensure_broker_traverse_acl "$CONFIG_ROOT" \
    || fail "could not preserve dispatch broker configuration access"
  if release_has_gateway_unit "$release_root"; then
    prepare_mcp_identity
    ensure_group_traverse_acl "$CONFIG_ROOT" "$MCP_HTTP_GROUP" \
      || fail "could not grant the HTTP gateway traverse access to $CONFIG_ROOT"
  fi

  if [[ ! -f "$CONFIG_ROOT/omcp.json" ]]; then
    sudo install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" \
      "$release_root/deploy/pixelbox/omcp.example.json" "$CONFIG_ROOT/omcp.json"
    echo "Installed example omcp.json; owner review is required before activation."
    STAGED_ONLY=1
  fi
  for env_file in omcp.env tunnel.env; do
    install_environment_file "$release_root" "$env_file" \
      "$SERVICE_USER" "$SERVICE_USER"
  done
  if release_has_gateway_unit "$release_root"; then
    install_environment_file "$release_root" mcp-http.env \
      "$MCP_HTTP_USER" "$MCP_HTTP_GROUP"
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

assert_no_gateway_listener() {
  command -v ss >/dev/null 2>&1 \
    || fail "ss is required to verify port 8090 cleanup"
  local listeners
  listeners="$(ss -ltnH)" || fail "could not inspect listeners during rollback"
  if awk '$4 ~ /:8090$/ { found=1 } END { exit !found }' <<<"$listeners"; then
    fail "a listener remains on port 8090 after gateway rollback"
  fi
}

stop_gateway_or_fail() {
  local active=0 enabled=0
  if sudo systemctl is-active --quiet orchestra-mcp-http.service 2>/dev/null; then
    active=1
  fi
  if sudo systemctl is-enabled --quiet orchestra-mcp-http.service 2>/dev/null; then
    enabled=1
  fi
  if (( active || enabled )); then
    sudo systemctl disable --now orchestra-mcp-http.service \
      || fail "could not stop/disable the HTTP gateway during rollback"
  fi
  sudo systemctl is-active --quiet orchestra-mcp-http.service 2>/dev/null \
    && fail "the HTTP gateway is still active after rollback"
  sudo systemctl is-enabled --quiet orchestra-mcp-http.service 2>/dev/null \
    && fail "the HTTP gateway is still enabled after rollback"
  assert_no_gateway_listener
  sudo rm -f "$UNIT_ROOT/orchestra-mcp-http.service"
  sudo rm -rf "$UNIT_ROOT/orchestra-mcp-http.service.d"
  sudo rm -f "$UNIT_ROOT/multi-user.target.wants/orchestra-mcp-http.service"
  sudo systemctl daemon-reload || fail "could not reload systemd after gateway removal"
}

install_units() {
  local release_root="$1" unit
  # Remove the newer gateway capability before installing other units so a
  # later missing-file failure cannot leave port 8090 active.
  if ! release_has_gateway_unit "$release_root"; then
    stop_gateway_or_fail
  fi
  for unit in orchestra-omcp.service orchestra-github-dispatch.service orchestra-tunnel.service \
              orchestra-health.service orchestra-health.timer \
              orchestra-backup.service orchestra-backup.timer \
              orchestra-alerts.service orchestra-alerts.timer \
              orchestra-dashboard.service orchestra-approval-portal.service; do
    if sudo test -f "$release_root/deploy/pixelbox/systemd/$unit"; then
      sudo install -m 0644 "$release_root/deploy/pixelbox/systemd/$unit" \
        "$UNIT_ROOT/$unit"
    else
      # Non-gateway capabilities are owner-managed outside this rollback. Do
      # not silently disable the tunnel, broker, or health services.
      echo "NOTE: target release has no $unit; leaving the current unit untouched." >&2
    fi
  done
  if release_has_gateway_unit "$release_root"; then
    local gateway_source
    gateway_source="$(gateway_unit_source "$release_root")"
    if [[ "$gateway_source" != "$release_root/deploy/pixelbox/systemd/orchestra-mcp-http.service" ]]; then
      echo "WARNING: replacing an older gateway unit with the invoking hardened unit." >&2
    fi
    sudo install -m 0644 "$gateway_source" \
      "$UNIT_ROOT/orchestra-mcp-http.service"
  fi
}

system_account_tool() {
  local tool="$1" candidate
  candidate="$(command -v "$tool" 2>/dev/null || true)"
  if [[ -z "$candidate" ]]; then
    for candidate in "/usr/sbin/$tool" "/sbin/$tool"; do
      [[ -x "$candidate" ]] && break
    done
  fi
  if [[ "$(type -t "$tool")" == "function" ]]; then
    printf "%s\\n" "$tool"
    return 0
  fi
  [[ -n "$candidate" && -x "$candidate" ]] \
    || fail "$tool is required to create the HTTPS MCP identity"
  printf "%s\\n" "$candidate"
}

prepare_mcp_identity() {
  local desired_gid primary_gid groupadd_bin useradd_bin
  command -v getent >/dev/null 2>&1 || fail "getent is required to manage the HTTPS MCP identity"
  groupadd_bin="$(system_account_tool groupadd)"
  useradd_bin="$(system_account_tool useradd)"
  if ! getent group "$MCP_HTTP_GROUP" >/dev/null 2>&1; then
    sudo "$groupadd_bin" --system "$MCP_HTTP_GROUP"
  fi
  desired_gid="$(getent group "$MCP_HTTP_GROUP" | awk -F: 'NR == 1 {print $3}')"
  [[ "$desired_gid" =~ ^[0-9]+$ ]] \
    || fail "could not resolve the $MCP_HTTP_GROUP group ID"
  if ! id "$MCP_HTTP_USER" >/dev/null 2>&1; then
    sudo "$useradd_bin" --system --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin --gid "$MCP_HTTP_GROUP" "$MCP_HTTP_USER"
  fi
  primary_gid="$(id -g "$MCP_HTTP_USER")"
  [[ "$primary_gid" == "$desired_gid" ]] \
    || fail "$MCP_HTTP_USER must use $MCP_HTTP_GROUP as its primary group"
}

gateway_environment_is_ready() {
  local env_file="$CONFIG_ROOT/mcp-http.env" user_uid group_gid
  [[ -f "$env_file" && ! -L "$env_file" ]] \
    || fail "required gateway configuration is missing or unsafe: $env_file"
  user_uid="$(id -u "$MCP_HTTP_USER")"
  group_gid="$(getent group "$MCP_HTTP_GROUP" | awk -F: 'NR == 1 {print $3}')"
  [[ "$(sudo stat -c '%a' "$env_file")" == "600" ]] \
    || fail "$env_file must have mode 0600"
  [[ "$(sudo stat -c '%u' "$env_file")" == "$user_uid" ]] \
    || fail "$env_file must be owned by $MCP_HTTP_USER"
  [[ "$(sudo stat -c '%g' "$env_file")" == "$group_gid" ]] \
    || fail "$env_file must have group $MCP_HTTP_GROUP"
  sudo -u "$MCP_HTTP_USER" /usr/bin/test -x "$CONFIG_ROOT" \
    || fail "$MCP_HTTP_USER cannot traverse $CONFIG_ROOT"
  sudo -u "$MCP_HTTP_USER" /usr/bin/test -r "$env_file" \
    || fail "$MCP_HTTP_USER cannot read $env_file"
  sudo -u "$MCP_HTTP_USER" /usr/bin/python3 - "$env_file" <<'PY' \
    || fail "$env_file does not contain a valid ORCHESTRA_MCP_HTTP_TOKEN"
import re
import sys
from pathlib import Path

raw = Path(sys.argv[1]).read_bytes()
if b"\r" in raw:
    raise SystemExit(1)
text = raw.decode("utf-8")
values = []
for line in text.split("\n"):
    if not line or line.startswith("#"):
        continue
    if not line.startswith("ORCHESTRA_MCP_HTTP_TOKEN="):
        raise SystemExit(1)
    values.append(line.split("=", 1)[1])
if (len(values) != 1
        or re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,}", values[0]) is None):
    raise SystemExit(1)
PY
}

wait_for_omcp_socket() {
  local attempt
  for attempt in {1..50}; do
    if sudo test -S /run/orchestra/omcp.sock 2>/dev/null \
        && sudo -u "$MCP_HTTP_USER" /usr/bin/test -r /run/orchestra/omcp.sock 2>/dev/null \
        && sudo -u "$MCP_HTTP_USER" /usr/bin/test -w /run/orchestra/omcp.sock 2>/dev/null; then
      return 0
    fi
    sleep 0.2
  done
  echo "ERROR: the HTTP gateway identity cannot read/write the coordinator socket" >&2
  return 1
}

wait_for_gateway_health() {
  local attempt verifier_args=()
  if [[ -e "$VERIFIER_PATH" ]]; then
    [[ -s "$VERIFIER_PATH" ]] \
      || { echo "ERROR: stable verifier is empty" >&2; return 1; }
    [[ -f "$VERIFIER_PATH.sha256" ]] \
      || { echo "ERROR: stable verifier digest is missing" >&2; return 1; }
    [[ "$(cat "$VERIFIER_PATH.sha256")" == "$(sha256sum "$VERIFIER_PATH" | awk '{print $1}')" ]] \
      || { echo "ERROR: stable verifier digest mismatch" >&2; return 1; }
    verifier_args=("$VERIFIER_PATH")
  else
    verifier_args=(-m omcp.http_gateway_verify)
  fi
  for attempt in {1..50}; do
    if (cd "$SOURCE_ROOT" && \
        sudo -u "$MCP_HTTP_USER" /usr/bin/python3 -E "${verifier_args[@]}" \
          --host 127.0.0.1 --port 8090 --cycles 1 \
          --env-file "$CONFIG_ROOT/mcp-http.env" >/dev/null 2>&1); then
      return 0
    fi
    sleep 0.2
  done
  echo "ERROR: the HTTP gateway did not pass an authenticated backend check" >&2
  return 1
}

preflight_activation() {
  local release_root="$RELEASES_ROOT/$RELEASE_SHA" mode broker_uid
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
  [[ "$UNIT_ROOT" == "/etc/systemd/system" ]] \
    || fail "activation requires ORCHESTRA_SYSTEMD_UNIT_ROOT=/etc/systemd/system"
  command -v systemctl >/dev/null || fail "systemctl is required"
  command -v ss >/dev/null || fail "ss is required to verify gateway listener cleanup"
  [[ -x /usr/bin/python3 ]] || fail "/usr/bin/python3 is required by the systemd units"
  validate_release_tree "$release_root"
  for file in omcp.json omcp.env tunnel.env; do
    [[ -f "$CONFIG_ROOT/$file" ]] || fail "required owner configuration is missing: $CONFIG_ROOT/$file"
    mode="$(sudo stat -c '%a' "$CONFIG_ROOT/$file")"
    [[ "$mode" == "600" ]] || fail "$CONFIG_ROOT/$file must have mode 0600"
  done
  if release_has_gateway_unit "$release_root"; then
    [[ "$MCP_HTTP_USER" == "orchestra-mcp-http" && "$MCP_HTTP_GROUP" == "orchestra-mcp" ]] \
      || fail "the gateway unit requires the reviewed orchestra-mcp-http/orchestra-mcp identity"
    prepare_mcp_identity
  fi

  [[ "$(sudo stat -c '%u' "$CONFIG_ROOT")" == "$(id -u "$SERVICE_USER")" \
     && "$(sudo stat -c '%g' "$CONFIG_ROOT")" == "$(id -g "$SERVICE_USER")" ]] \
    || fail "$CONFIG_ROOT must be owned by $SERVICE_USER"

  # Keep an existing credential-isolated broker deployment intact across
  # activate and rollback as well as staging.
  ensure_broker_traverse_acl "$CONFIG_ROOT" \
    || fail "could not preserve dispatch broker configuration access"
  if id "$BROKER_USER" >/dev/null 2>&1 && sudo test -d "$CONFIG_ROOT/github-dispatch"; then
    broker_uid="$(id -u "$BROKER_USER")"
    [[ "$(sudo stat -c '%a' "$CONFIG_ROOT/github-dispatch")" == "700" ]] \
      || fail "$CONFIG_ROOT/github-dispatch must have mode 0700"
    [[ "$(sudo stat -c '%u' "$CONFIG_ROOT/github-dispatch")" == "$broker_uid" ]] \
      || fail "$CONFIG_ROOT/github-dispatch must be owned by $BROKER_USER"
    sudo -u "$BROKER_USER" /usr/bin/test -x "$CONFIG_ROOT" \
      || fail "$BROKER_USER cannot traverse $CONFIG_ROOT"
    for file in github-dispatch/config.json github-dispatch/github-actions.token; do
      sudo test -f "$CONFIG_ROOT/$file" || fail "broker configuration is missing: $CONFIG_ROOT/$file"
      [[ "$(sudo stat -c '%a' "$CONFIG_ROOT/$file")" == "600" ]] \
        || fail "$CONFIG_ROOT/$file must have mode 0600"
      [[ "$(sudo stat -c '%u' "$CONFIG_ROOT/$file")" == "$broker_uid" ]] \
        || fail "$CONFIG_ROOT/$file must be owned by $BROKER_USER"
      sudo -u "$BROKER_USER" /usr/bin/test -r "$CONFIG_ROOT/$file" \
        || fail "$BROKER_USER cannot read $CONFIG_ROOT/$file"
    done
  fi
  if release_has_gateway_unit "$release_root"; then
    ensure_group_traverse_acl "$CONFIG_ROOT" "$MCP_HTTP_GROUP" \
      || fail "could not grant the HTTP gateway traverse access to $CONFIG_ROOT"
    gateway_environment_is_ready
  fi
}


preflight_activation_config() {
  local release_root="$1"
  sudo -u "$SERVICE_USER" env PYTHONPATH="$release_root" /usr/bin/python3 \
    "$SOURCE_ROOT/deploy/pixelbox/config_preflight.py" \
    --config "$CONFIG_ROOT/omcp.json" \
    --config-root "$CONFIG_ROOT" \
    --release-root "$release_root" \
    --service-user "$SERVICE_USER" \
    --expected-sha "$RELEASE_SHA" \
    --free-min-bytes 536870912 \
    || fail "activation preflight failed; no services were restarted"
}

backup_activation_config() {
  local old_sha="$1" stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  CONFIG_BACKUP_DIR="$STATE_ROOT.parent/backup/pre-activate-${RELEASE_SHA}-${stamp}"
  install -d -m 0700 "$CONFIG_BACKUP_DIR" \
    || fail "could not create the activation config backup directory"
  sudo tar --acls --xattrs -C /etc -czf "$CONFIG_BACKUP_DIR/etc-orchestra.tar.gz" orchestra \
    || fail "could not back up the owner configuration before activation"
  sudo chmod 0600 "$CONFIG_BACKUP_DIR/etc-orchestra.tar.gz"
  printf '%s\n' "$old_sha" > "$CONFIG_BACKUP_DIR/previous-release"
  chmod 0600 "$CONFIG_BACKUP_DIR/previous-release"
  CONFIG_BACKUP_ARCHIVE="$CONFIG_BACKUP_DIR/etc-orchestra.tar.gz"
  echo "Activation config backup: $CONFIG_BACKUP_DIR"
}

restore_activation_config() {
  [[ -n "$CONFIG_BACKUP_ARCHIVE" && -f "$CONFIG_BACKUP_ARCHIVE" ]] \
    || fail "activation failed and no matching prior config backup is available"
  sudo tar --acls --xattrs -C /etc -xzf "$CONFIG_BACKUP_ARCHIVE" \
    || fail "activation failed and the prior configuration could not be restored"
}

service_active_if_installed() {
  local unit="$1"
  if systemctl list-unit-files --no-legend "$unit" 2>/dev/null | grep -q "^$unit"; then
    sudo systemctl is-active --quiet "$unit"
  fi
}

activate_release() {
  local release_root="$RELEASES_ROOT/$RELEASE_SHA" old_sha="" old_root=""
  local CONFIG_BACKUP_DIR="" CONFIG_BACKUP_ARCHIVE=""
  old_sha="$(release_sha_from_link "$CURRENT_LINK" || true)"
  if [[ -n "$old_sha" ]]; then
    old_root="$RELEASES_ROOT/$old_sha"
  elif [[ -e "$CURRENT_LINK" ]]; then
    fail "$CURRENT_LINK exists but is not a managed release symlink"
  fi

  preflight_activation_config "$release_root"
  backup_activation_config "$old_sha"
  install_units "$release_root"
  if release_has_gateway_unit "$release_root"; then
    prepare_mcp_identity
    install_gateway_verifier
  fi
  sudo systemctl daemon-reload
  atomic_symlink "releases/$RELEASE_SHA" "$CURRENT_LINK"

  if PYTHONPATH="$release_root" /usr/bin/python3 -c \
        'import sys; from pathlib import Path; from omcp.controller import Config; Config.load(Path(sys.argv[1]))' \
        "$CONFIG_ROOT/omcp.json" \
      && sudo systemctl enable orchestra-omcp.service \
      && sudo systemctl enable --now orchestra-health.timer \
      && sudo systemctl restart orchestra-omcp.service \
      && sudo systemctl restart orchestra-github-dispatch.service \
      && sudo systemctl is-active --quiet orchestra-github-dispatch.service \
      && { ! release_has_gateway_unit "$release_root" || wait_for_omcp_socket; } \
      && { ! release_has_gateway_unit "$release_root" || { sudo systemctl enable orchestra-mcp-http.service && sudo systemctl restart orchestra-mcp-http.service && sudo systemctl is-active --quiet orchestra-mcp-http.service; }; } \
      && { ! release_has_gateway_unit "$release_root" || { wait_for_gateway_health && sudo systemctl is-active --quiet orchestra-mcp-http.service; }; } \
      && sudo systemctl is-active --quiet orchestra-omcp.service \
      && sudo systemctl is-active --quiet orchestra-health.timer \
      && service_active_if_installed orchestra-dashboard.service \
      && service_active_if_installed orchestra-tunnel.service \
      && { ! release_has_gateway_unit "$release_root" || sudo "$release_root/deploy/pixelbox/verify_mcp_http.sh" >/dev/null; }; then
    if [[ -n "$old_sha" && "$old_sha" != "$RELEASE_SHA" ]]; then
      atomic_symlink "releases/$old_sha" "$PREVIOUS_LINK"
    fi
    echo "Activated exact release $RELEASE_SHA."
    echo "Tunnel remains unchanged and must be enabled only after separate verification."
    return
  fi

  echo "Activation failed; restoring the prior release and configuration." >&2
  if [[ -n "$old_sha" && -d "$old_root" ]]; then
    restore_activation_config
    atomic_symlink "releases/$old_sha" "$CURRENT_LINK"
    install_units "$old_root"
    sudo systemctl daemon-reload
    sudo systemctl restart orchestra-omcp.service \
      || fail "activation failed and the prior coordinator could not restart"
    sudo systemctl restart orchestra-github-dispatch.service \
      || fail "activation failed and the prior GitHub dispatch broker could not restart"
    sudo systemctl start orchestra-health.timer \
      || fail "activation failed and the prior health timer could not restart"
    if release_has_gateway_unit "$old_root"; then
      sudo systemctl enable orchestra-mcp-http.service \
        || fail "activation failed and the prior gateway could not be re-enabled"
      sudo systemctl restart orchestra-mcp-http.service \
        || fail "activation failed and the prior gateway could not restart"
      wait_for_omcp_socket || fail "activation failed and the prior gateway socket is unavailable"
      wait_for_gateway_health || fail "activation failed and the prior gateway is unhealthy"
    fi
    fail "activation failed; current was restored to $old_sha"
  fi
  restore_activation_config
  sudo unlink "$CURRENT_LINK" 2>/dev/null || true
  sudo systemctl stop orchestra-mcp-http.service 2>/dev/null || true
  stop_gateway_or_fail
  for unit in orchestra-omcp.service orchestra-health.timer; do
    if sudo systemctl is-active --quiet "$unit" 2>/dev/null; then
      sudo systemctl stop "$unit" || fail "first activation failed and $unit could not stop"
    fi
    if sudo systemctl is-enabled --quiet "$unit" 2>/dev/null; then
      sudo systemctl disable "$unit" || fail "first activation failed and $unit could not be disabled"
    fi
    sudo systemctl is-active --quiet "$unit" 2>/dev/null \
      && fail "first activation failed and $unit is still active"
    sudo systemctl is-enabled --quiet "$unit" 2>/dev/null \
      && fail "first activation failed and $unit is still enabled"
  done
  sudo systemctl daemon-reload || fail "first activation failed and systemd reload failed"
  fail "first activation failed; no prior managed release was available"
}

main() {
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
    return 0
  fi

  [[ "${ORCHESTRA_DEPLOY_APPROVED:-0}" == "1" ]] \
    || fail "deployment staging is not approved; set ORCHESTRA_DEPLOY_APPROVED=1 only after approval"
  [[ "$(id -un)" == "$SERVICE_USER" ]] || fail "run as $SERVICE_USER"
  command -v sudo >/dev/null || fail "sudo is required"
  command -v tar >/dev/null || fail "tar is required"
  command -v python3 >/dev/null || fail "python3 is required"
  trap cleanup EXIT

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
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
