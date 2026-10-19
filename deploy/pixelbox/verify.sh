#!/usr/bin/env bash
# Read-only deployment verification for PixelBox.  Safe to run repeatedly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="${ORCHESTRA_CONFIG:-/etc/orchestra/omcp.json}"
FAILURES=0
SKIPPED=0

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILURES=$((FAILURES + 1)); }
skip() { echo "SKIP  $*"; SKIPPED=$((SKIPPED + 1)); }

echo "Orchestra PixelBox verification"
echo "source: $ROOT"
echo "config: $CONFIG"
echo

if command -v python3 >/dev/null; then
  if python3 -m compileall -q "$ROOT/omcp" "$ROOT/mac_bridge" "$ROOT/tests" "$ROOT/scripts"; then
    pass "Python sources compile"
  else
    fail "Python compilation"
  fi
  if (cd "$ROOT" && python3 -m unittest discover -s tests -q); then
    pass "unit and integration tests"
  else
    fail "unit and integration tests"
  fi
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
  if (cd "$ROOT" && python3 -m omcp.health --config "$CONFIG" --json --allow-degraded >"$health_file"); then
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
  skip "configuration not installed at $CONFIG"
fi

if command -v systemctl >/dev/null; then
  for unit in orchestra-omcp.service orchestra-health.timer; do
    if systemctl is-active --quiet "$unit"; then
      pass "$unit active"
    else
      skip "$unit not active (staging or not installed)"
    fi
  done
  if systemctl is-active --quiet orchestra-tunnel.service; then
    pass "orchestra-tunnel.service active"
  else
    skip "orchestra-tunnel.service not enabled (expected before approved cutover)"
  fi
  echo
  echo "Existing services observed but not modified:"
  systemctl list-units --type=service --state=running --no-legend --no-pager \
    'nextcloud*' 'docker*' 'tailscaled*' 2>/dev/null || true
else
  skip "systemctl unavailable"
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
  skip "ORCHESTRA_TUNNEL_HEALTH_URL is not set"
fi

echo
if (( FAILURES )); then
  echo "Verification failed: $FAILURES check(s), $SKIPPED skipped."
  exit 1
fi
echo "Verification passed with $SKIPPED skipped check(s)."
