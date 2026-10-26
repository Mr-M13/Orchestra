#!/usr/bin/env bash
# Read-only deployment verification for PixelBox.  Safe to run repeatedly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
CONFIG="${ORCHESTRA_CONFIG:-/etc/orchestra/omcp.json}"
INSTALL_ROOT="${ORCHESTRA_INSTALL_ROOT:-/opt/orchestra}"
FAILURES=0
SKIPPED=0
UNCHECKED=0
LIVE=0
WITH_TESTS=0
REQUIRE_HEALTH=0
EXPECTED_SHA=""
MCP_SOCKET="${ORCHESTRA_MCP_SOCKET:-/home/hamed/orchestra-migration/state/mcp.sock}"

usage() {
  cat <<'EOF'
Usage: verify.sh [--live] [--sha <40-hex-sha>] [--with-tests] [--require-health]

Default mode performs source and staging checks without requiring installed or
active services. --live requires installed configuration, coordinator, health
timer, tunnel and the expected exact active release to pass. Optional external
health probes are reported as UNCHECKED unless --require-health makes them
fatal. Live mode validates the deployed release and services; it does not run
the source-tree unit suite unless --with-tests is also supplied.
EOF
}

while (($#)); do
  case "$1" in
    --live) LIVE=1 ;;
    --with-tests) WITH_TESTS=1 ;;
    --require-health) REQUIRE_HEALTH=1 ;;
    --sha)
      shift
      (($#)) || { echo "--sha requires a value" >&2; exit 2; }
      EXPECTED_SHA="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ -n "$EXPECTED_SHA" && ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "--sha must be exactly 40 lowercase hexadecimal characters" >&2
  exit 2
fi
if [[ "$LIVE" == "1" && -z "$EXPECTED_SHA" ]]; then
  echo "--live requires --sha so the intended release is verified" >&2
  exit 2
fi

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILURES=$((FAILURES + 1)); }
skip() { echo "SKIP  $*"; SKIPPED=$((SKIPPED + 1)); }
unchecked() { echo "UNCHECKED  $*"; UNCHECKED=$((UNCHECKED + 1)); }

release_digest() {
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

echo "Orchestra PixelBox verification"
echo "source: $ROOT"
echo "config: $CONFIG"
echo

if [[ "$(uname -s)" == "Linux" && ( "$(uname -m)" == "aarch64" || "$(uname -m)" == "arm64" ) ]]; then
  pass "Linux ARM64 host"
elif [[ "$LIVE" == "1" ]]; then
  fail "live verification requires Linux ARM64"
else
  skip "host is not Linux ARM64; only portable source checks are valid"
fi

if [[ -r "$ROOT/.orchestra-release-sha" ]]; then
  ACTUAL_SHA="$(<"$ROOT/.orchestra-release-sha")"
  if [[ "$ACTUAL_SHA" =~ ^[0-9a-f]{40}$ && "$(basename "$ROOT")" == "$ACTUAL_SHA" ]]; then
    pass "versioned release marker $ACTUAL_SHA"
  else
    fail "release directory and marker do not identify the same exact SHA"
  fi
  if [[ -n "$EXPECTED_SHA" && "$ACTUAL_SHA" != "$EXPECTED_SHA" ]]; then
    fail "active release $ACTUAL_SHA does not match expected $EXPECTED_SHA"
  fi
  if [[ -L "$INSTALL_ROOT/current" && "$(readlink "$INSTALL_ROOT/current")" == "releases/$ACTUAL_SHA" ]]; then
    pass "atomic current symlink selects the marked release"
  elif [[ "$LIVE" == "1" ]]; then
    fail "$INSTALL_ROOT/current does not select the marked release"
  else
    skip "$INSTALL_ROOT/current does not select this staged release"
  fi
  if [[ -r "$ROOT/.orchestra-release-digest" ]]; then
    RECORDED_DIGEST="$(<"$ROOT/.orchestra-release-digest")"
    ACTUAL_DIGEST="$(release_digest "$ROOT" 2>/dev/null || true)"
    if [[ "$RECORDED_DIGEST" =~ ^[0-9a-f]{64}$ && "$ACTUAL_DIGEST" == "$RECORDED_DIGEST" ]]; then
      pass "release tree matches its validated digest"
    else
      fail "release tree differs from its validated digest"
    fi
  elif [[ "$LIVE" == "1" ]]; then
    fail "release digest is missing"
  else
    skip "release digest is missing from this staging directory"
  fi
elif [[ "$LIVE" == "1" ]]; then
  fail "versioned release marker is missing"
else
  skip "source checkout is not an installed versioned release"
fi

if command -v python3 >/dev/null; then
  CACHE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/orchestra-verify-pycache.XXXXXX")"
  if PYTHONPYCACHEPREFIX="$CACHE_ROOT" python3 -m compileall -q \
      "$ROOT/omcp" "$ROOT/mac_bridge" "$ROOT/tests" "$ROOT/scripts"; then
    pass "Python sources compile"
  else
    fail "Python compilation"
  fi
  if [[ "$LIVE" == "1" && "$WITH_TESTS" == "0" ]]; then
    unchecked "source-tree unit tests (live mode; use --with-tests to run them)"
  elif (cd "$ROOT" && PYTHONPYCACHEPREFIX="$CACHE_ROOT" \
      python3 -m unittest discover -s tests -q); then
    pass "unit and integration tests"
  else
    fail "unit and integration tests"
  fi
  rm -rf -- "$CACHE_ROOT"
else
  fail "python3 is unavailable"
fi

if [[ -r "$CONFIG" ]]; then
  if [[ "$(stat -c '%a' "$CONFIG" 2>/dev/null || stat -f '%Lp' "$CONFIG")" == "600" ]]; then
    pass "configuration is owner-only"
  else
    fail "configuration mode is not 0600"
  fi
  health_file="$(umask 077 && mktemp "${TMPDIR:-/tmp}/orchestra-health.XXXXXX")"
  health_args=(--config "$CONFIG" --json --allow-degraded)
  backup_config="${ORCHESTRA_BACKUP_CONFIG:-/etc/orchestra/backup.json}"
  [[ -n "$backup_config" ]] && health_args+=(--backup-config "$backup_config")
  alerts_config="${ORCHESTRA_ALERTS_CONFIG:-/etc/orchestra/alerts.json}"
  [[ -n "$alerts_config" ]] && health_args+=(--alerts-config "$alerts_config")
  [[ -n "${ORCHESTRA_TUNNEL_HEALTH_URL:-}" ]] \
    && health_args+=(--tunnel-health-url "$ORCHESTRA_TUNNEL_HEALTH_URL")
  [[ -n "${ORCHESTRA_DASHBOARD_HEALTH_URL:-}" ]] \
    && health_args+=(--dashboard-health-url "$ORCHESTRA_DASHBOARD_HEALTH_URL")
  [[ -n "${ORCHESTRA_MCP_HEALTH_URL:-}" ]] \
    && health_args+=(--mcp-health-url "$ORCHESTRA_MCP_HEALTH_URL")
  [[ -n "${ORCHESTRA_BACKUP_DIR:-}" ]] \
    && health_args+=(--backup-dir "$ORCHESTRA_BACKUP_DIR")
  if (cd "$ROOT" && python3 -m omcp.health "${health_args[@]}" >"$health_file"); then
    health_status="$(python3 - "$health_file" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
print(value.get("status", "failed"))
print(f"      status={value.get('status')} platform={value.get('platform')}", file=sys.stderr)
for warning in value.get("warnings", []):
    print(f"      warning={warning}", file=sys.stderr)
for name, check in sorted(value.get("checks", {}).items()):
    if isinstance(check, dict) and check.get("checked") is False:
        print(f"      unchecked: {name} ({check.get('status', 'unchecked')})", file=sys.stderr)
PY
)"
    if [[ "$health_status" == "ok" ]]; then
      pass "health report"
    elif [[ "$health_status" == "degraded" && "$REQUIRE_HEALTH" == "0" ]]; then
      pass "health report degraded (warnings shown; use --require-health to fail)"
    else
      fail "health report status=$health_status"
    fi
  else
    fail "health report (see python3 -m omcp.health --config $CONFIG)"
  fi
  rm -f "$health_file"
else
  if [[ "$LIVE" == "1" ]]; then
    fail "configuration not installed at $CONFIG"
  else
    skip "configuration not installed at $CONFIG"
  fi
fi

if command -v systemctl >/dev/null; then
  for unit in orchestra-omcp.service orchestra-health.timer; do
    if systemctl is-active --quiet "$unit"; then
      pass "$unit active"
    else
      if [[ "$LIVE" == "1" ]]; then
        fail "$unit is not active"
      else
        skip "$unit not active (staging or not installed)"
      fi
    fi
  done
  for unit in orchestra-dashboard.service orchestra-github-dispatch.service; do
    if systemctl list-unit-files --no-legend "$unit" 2>/dev/null | grep -q "^$unit"; then
      if systemctl is-active --quiet "$unit"; then
        pass "$unit active"
      elif [[ "$LIVE" == "1" ]]; then
        fail "$unit is installed but not active"
      else
        skip "$unit installed but not active"
      fi
    else
      unchecked "$unit is not installed"
    fi
  done
  if [[ -S "$MCP_SOCKET" ]]; then
    socket_mode="$(stat -c '%a' "$MCP_SOCKET" 2>/dev/null || stat -f '%Lp' "$MCP_SOCKET")"
    case "$socket_mode" in
      600|660)
        pass "authenticated MCP socket is owner/group-only"
        ;;
      *)
        fail "MCP socket mode is $socket_mode, expected 600 or 660"
        ;;
    esac
  elif [[ "$LIVE" == "1" ]]; then
    fail "MCP socket is missing at $MCP_SOCKET"
  else
    skip "MCP socket is not present"
  fi
  if systemctl is-active --quiet orchestra-tunnel.service; then
    pass "orchestra-tunnel.service active"
  else
    if [[ "$LIVE" == "1" ]]; then
      fail "orchestra-tunnel.service is not active"
    else
      skip "orchestra-tunnel.service not enabled (expected before approved cutover)"
    fi
  fi
  echo
  echo "Existing services observed but not modified:"
  systemctl list-units --type=service --state=running --no-legend --no-pager \
    'nextcloud*' 'docker*' 'tailscaled*' 2>/dev/null || true
else
  if [[ "$LIVE" == "1" ]]; then
    fail "systemctl unavailable"
  else
    skip "systemctl unavailable"
  fi
fi

if command -v tailscale >/dev/null; then
  echo
  echo "Tailscale Serve (read-only observation):"
  tailscale serve status 2>/dev/null || true
fi

if [[ -n "${ORCHESTRA_TUNNEL_HEALTH_URL:-}" ]]; then
  if curl --fail --silent --show-error --max-time 5 "$ORCHESTRA_TUNNEL_HEALTH_URL" >/dev/null; then
    pass "tunnel health endpoint"
  else
    fail "tunnel health endpoint"
  fi
else
  if [[ "$LIVE" == "1" ]]; then
    fail "ORCHESTRA_TUNNEL_HEALTH_URL is not set"
  else
    skip "ORCHESTRA_TUNNEL_HEALTH_URL is not set"
  fi
fi

for name in DASHBOARD MCP; do
  variable="ORCHESTRA_${name}_HEALTH_URL"
  url="${!variable:-}"
  if [[ -z "$url" ]]; then
    unchecked "$variable is not set"
    continue
  fi
  if curl --fail --silent --show-error --max-time 5 "$url" >/dev/null; then
    pass "$name health endpoint"
  else
    fail "$name health endpoint"
  fi
done

echo
if (( FAILURES )); then
  echo "Verification failed: $FAILURES check(s), $SKIPPED skipped, $UNCHECKED unchecked."
  exit 1
fi
if (( REQUIRE_HEALTH && UNCHECKED )); then
  echo "Verification incomplete: $UNCHECKED required health probe(s) were not checked."
  exit 2
fi
if (( UNCHECKED )); then
  echo "Verification PARTIAL: $SKIPPED skipped, $UNCHECKED unchecked. Set optional health URLs or use --require-health for a strict pass."
else
  echo "Verification passed with $SKIPPED skipped check(s)."
fi
