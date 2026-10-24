#!/usr/bin/env bash
# Read-only live verifier. It never prints ORCHESTRA_MCP_HTTP_TOKEN.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SYSTEMCTL="$(command -v systemctl || true)"
SS="$(command -v ss || true)"
STAT="$(command -v stat || true)"
GETENT="$(command -v getent || true)"
ID="$(command -v id || true)"
GREP="$(command -v grep || true)"
AWK="$(command -v awk || true)"
SUDO="$(command -v sudo || true)"
SHA256SUM="$(command -v sha256sum || true)"
for tool in SYSTEMCTL SS STAT GETENT ID GREP AWK SUDO SHA256SUM; do
  [[ -n "${!tool}" ]] || { echo "required verifier tool is unavailable: $tool" >&2; exit 1; }
done

ENV_FILE="${ORCHESTRA_MCP_HTTP_ENV:-/etc/orchestra/mcp-http.env}"
MCP_HTTP_USER="${ORCHESTRA_MCP_HTTP_USER:-orchestra-mcp-http}"
MCP_HTTP_GROUP="${ORCHESTRA_MCP_HTTP_GROUP:-orchestra-mcp}"
OMCP_USER="${ORCHESTRA_SERVICE_USER:-hamed}"
STABLE_VERIFIER="${ORCHESTRA_VERIFIER_PATH:-/usr/local/lib/orchestra/http_gateway_verify.py}"
[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] \
  || { echo "missing or unsafe gateway environment" >&2; exit 1; }
[[ "$("$STAT" -c '%a' "$ENV_FILE" 2>/dev/null || "$STAT" -f '%Lp' "$ENV_FILE")" == "600" ]] \
  || { echo "gateway environment must have mode 0600" >&2; exit 1; }
EXPECTED_UID="$("$ID" -u "$MCP_HTTP_USER")"
EXPECTED_GID="$("$GETENT" group "$MCP_HTTP_GROUP" | "$AWK" -F: 'NR == 1 {print $3}')"
OMCP_UID="$("$ID" -u "$OMCP_USER")"
[[ -n "$EXPECTED_GID" && -n "$OMCP_UID" ]] \
  || { echo "gateway or coordinator identity is unavailable" >&2; exit 1; }
[[ "$("$STAT" -c '%u' "$ENV_FILE" 2>/dev/null || "$STAT" -f '%u' "$ENV_FILE")" == "$EXPECTED_UID" ]] \
  || { echo "gateway environment must be owned by $MCP_HTTP_USER" >&2; exit 1; }
[[ "$("$STAT" -c '%g' "$ENV_FILE" 2>/dev/null || "$STAT" -f '%g' "$ENV_FILE")" == "$EXPECTED_GID" ]] \
  || { echo "gateway environment must have group $MCP_HTTP_GROUP" >&2; exit 1; }

read_token() {
  if [[ "$("$ID" -un)" == "$MCP_HTTP_USER" ]]; then
    /usr/bin/python3 -E - "$ENV_FILE"
  else
    "$SUDO" -u "$MCP_HTTP_USER" /usr/bin/python3 -E - "$ENV_FILE"
  fi <<'PY'
import re
import sys
from pathlib import Path

values = []
for line in Path(sys.argv[1]).read_text().splitlines():
    if not line or line.startswith("#"):
        continue
    if not line.startswith("ORCHESTRA_MCP_HTTP_TOKEN="):
        raise SystemExit(1)
    values.append(line.split("=", 1)[1])
if (len(values) != 1
        or re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,}", values[0]) is None):
    raise SystemExit(1)
print(values[0])
PY
}

read_token >/dev/null \
  || { echo "gateway environment does not contain a valid token" >&2; exit 1; }

"$SYSTEMCTL" is-active --quiet orchestra-mcp-http.service
"$SYSTEMCTL" is-enabled --quiet orchestra-mcp-http.service
UNIT_SHOW="$("$SYSTEMCTL" show orchestra-mcp-http.service \
  -p User -p Group -p NoNewPrivileges -p ProtectSystem -p ProtectHome \
  -p PrivateNetwork -p RestrictAddressFamilies -p PartOf \
  -p IPAddressDeny -p IPAddressAllow -p SocketBindDeny -p SocketBindAllow)"
for expected in \
  "User=orchestra-mcp-http" \
  "Group=orchestra-mcp" \
  "NoNewPrivileges=yes" \
  "ProtectSystem=strict" \
  "ProtectHome=yes" \
  "PrivateNetwork=no" \
  "RestrictAddressFamilies=AF_UNIX AF_INET" \
  "PartOf=orchestra-omcp.service" \
  "IPAddressDeny=any" \
  "IPAddressAllow=127.0.0.1/32" \
  "SocketBindDeny=any" \
  "SocketBindAllow=ipv4:tcp:8090"; do
  "$GREP" -qx "$expected" <<<"$UNIT_SHOW" \
    || { echo "gateway unit property mismatch: $expected" >&2; exit 1; }
done
UNIT_PATH="$("$SYSTEMCTL" show orchestra-mcp-http.service -p FragmentPath --value)"
[[ -n "$UNIT_PATH" && -f "$UNIT_PATH" ]] \
  || { echo "gateway unit file is unavailable" >&2; exit 1; }
[[ "$("$STAT" -c '%u' "$UNIT_PATH" 2>/dev/null || "$STAT" -f '%u' "$UNIT_PATH")" == "0" \
   && "$("$STAT" -c '%a' "$UNIT_PATH" 2>/dev/null || "$STAT" -f '%Lp' "$UNIT_PATH")" == "644" ]] \
  || { echo "gateway unit file must be root-owned mode 0644" >&2; exit 1; }
LISTENERS="$("$SS" -ltnH)" || { echo "could not inspect listening sockets" >&2; exit 1; }
LOCAL_ADDRESSES="$("$AWK" '{print $4}' <<<"$LISTENERS")"
"$GREP" -qx '127\.0\.0\.1:8090' <<<"$LOCAL_ADDRESSES" \
  || { echo "gateway is not listening on 127.0.0.1:8090" >&2; exit 1; }
if "$GREP" -E '(^|:)8090$' <<<"$LOCAL_ADDRESSES" | "$GREP" -vqx '127\.0\.0\.1:8090'; then
  echo "gateway is listening on a non-loopback address" >&2
  exit 1
fi
RUNTIME_METADATA="$("$STAT" -c '%u %g %a' /run/orchestra 2>/dev/null \
  || "$STAT" -f '%u %g %Lp' /run/orchestra)"
read -r RUNTIME_UID RUNTIME_GID RUNTIME_MODE <<<"$RUNTIME_METADATA"
[[ "$RUNTIME_UID" == "$OMCP_UID" && "$RUNTIME_GID" == "$EXPECTED_GID" && "$RUNTIME_MODE" == "710" ]] \
  || { echo "coordinator runtime directory identity/mode does not match policy" >&2; exit 1; }
SOCKET_METADATA="$("$STAT" -c '%u %g %a' /run/orchestra/omcp.sock 2>/dev/null \
  || "$STAT" -f '%u %g %Lp' /run/orchestra/omcp.sock)"
read -r SOCKET_UID SOCKET_GID SOCKET_MODE <<<"$SOCKET_METADATA"
[[ "$SOCKET_UID" == "$OMCP_UID" && "$SOCKET_GID" == "$EXPECTED_GID" && "$SOCKET_MODE" == "660" ]] \
  || { echo "coordinator socket identity/mode does not match policy" >&2; exit 1; }
"$SUDO" -u "$MCP_HTTP_USER" /usr/bin/test -r /run/orchestra/omcp.sock
"$SUDO" -u "$MCP_HTTP_USER" /usr/bin/test -w /run/orchestra/omcp.sock
if [[ -f "$STABLE_VERIFIER" ]]; then
  [[ "$("$STAT" -c '%u' "$STABLE_VERIFIER" 2>/dev/null || "$STAT" -f '%u' "$STABLE_VERIFIER")" == "0" \
     && "$("$STAT" -c '%a' "$STABLE_VERIFIER" 2>/dev/null || "$STAT" -f '%Lp' "$STABLE_VERIFIER")" == "644" \
     && "$("$STAT" -c '%s' "$STABLE_VERIFIER" 2>/dev/null || "$STAT" -f '%z' "$STABLE_VERIFIER")" -gt 0 ]] \
    || { echo "stable verifier metadata is unsafe" >&2; exit 1; }
  [[ -f "$STABLE_VERIFIER.sha256" ]] \
    || { echo "stable verifier digest is missing" >&2; exit 1; }
  EXPECTED_VERIFIER_HASH="$("$AWK" 'NR == 1 {print $1}' "$STABLE_VERIFIER.sha256")"
  ACTUAL_VERIFIER_HASH="$("$SHA256SUM" "$STABLE_VERIFIER" | "$AWK" '{print $1}')"
  [[ "$EXPECTED_VERIFIER_HASH" == "$ACTUAL_VERIFIER_HASH" ]] \
    || { echo "stable verifier digest mismatch" >&2; exit 1; }
fi
(
  cd "$ROOT"
  if [[ -f "$STABLE_VERIFIER" ]]; then
    "$SUDO" -u "$MCP_HTTP_USER" /usr/bin/python3 -E "$STABLE_VERIFIER" \
      --host 127.0.0.1 --port 8090 --cycles 20 --env-file "$ENV_FILE"
  else
    "$SUDO" -u "$MCP_HTTP_USER" /usr/bin/python3 -E -m omcp.http_gateway_verify \
      --host 127.0.0.1 --port 8090 --cycles 20 --env-file "$ENV_FILE"
  fi
)
