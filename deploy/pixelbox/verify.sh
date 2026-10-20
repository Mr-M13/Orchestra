#!/usr/bin/env bash
# Read-only deployment verification for PixelBox.  Safe to run repeatedly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
CONFIG="${ORCHESTRA_CONFIG:-/etc/orchestra/omcp.json}"
INSTALL_ROOT="${ORCHESTRA_INSTALL_ROOT:-/opt/orchestra}"
FAILURES=0
SKIPPED=0
LIVE=0
EXPECTED_SHA=""

usage() {
  cat <<'EOF'
Usage: verify.sh [--live] [--sha <40-hex-sha>]

Default mode performs source and staging checks without requiring installed or
active services. --live requires installed configuration, coordinator, health
timer, tunnel, tunnel health URL, and the expected exact active release to pass.
EOF
}

while (($#)); do
  case "$1" in
    --live) LIVE=1 ;;
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
  if (cd "$ROOT" && PYTHONPYCACHEPREFIX="$CACHE_ROOT" \
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
  health_args=(--config "$CONFIG" --json)
  if [[ "$LIVE" == "0" ]]; then
    health_args+=(--allow-degraded)
  fi
  if (cd "$ROOT" && python3 -m omcp.health "${health_args[@]}" >"$health_file"); then
    pass "health report"
    python3 - "$health_file" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
print(f"      status={value.get('status')} platform={value.get('platform')}")
PY
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

echo
if (( FAILURES )); then
  echo "Verification failed: $FAILURES check(s), $SKIPPED skipped."
  exit 1
fi
echo "Verification passed with $SKIPPED skipped check(s)."
