#!/usr/bin/env bash
# Read-only live verifier. It never prints ORCHESTRA_MCP_HTTP_TOKEN.
set -euo pipefail

ENV_FILE="${ORCHESTRA_MCP_HTTP_ENV:-/etc/orchestra/mcp-http.env}"
MCP_HTTP_USER="${ORCHESTRA_MCP_HTTP_USER:-orchestra-mcp-http}"
MCP_HTTP_GROUP="${ORCHESTRA_MCP_HTTP_GROUP:-orchestra-mcp}"
OMCP_USER="${ORCHESTRA_SERVICE_USER:-hamed}"
[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] \
  || { echo "missing or unsafe gateway environment" >&2; exit 1; }
[[ "$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE")" == "600" ]] \
  || { echo "gateway environment must have mode 0600" >&2; exit 1; }
command -v getent >/dev/null || { echo "getent is required" >&2; exit 1; }
EXPECTED_UID="$(id -u "$MCP_HTTP_USER")"
EXPECTED_GID="$(getent group "$MCP_HTTP_GROUP" | awk -F: 'NR == 1 {print $3}')"
OMCP_UID="$(id -u "$OMCP_USER")"
[[ "$(stat -c '%u' "$ENV_FILE" 2>/dev/null || stat -f '%u' "$ENV_FILE")" == "$EXPECTED_UID" ]] \
  || { echo "gateway environment must be owned by $MCP_HTTP_USER" >&2; exit 1; }
[[ "$(stat -c '%g' "$ENV_FILE" 2>/dev/null || stat -f '%g' "$ENV_FILE")" == "$EXPECTED_GID" ]] \
  || { echo "gateway environment must have group $MCP_HTTP_GROUP" >&2; exit 1; }

read_token() {
  if [[ "$(id -un)" == "$MCP_HTTP_USER" ]]; then
    /usr/bin/python3 - "$ENV_FILE"
  else
    sudo -u "$MCP_HTTP_USER" /usr/bin/python3 - "$ENV_FILE"
  fi <<'PY'
import sys
from pathlib import Path

value = None
for raw in Path(sys.argv[1]).read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("ORCHESTRA_MCP_HTTP_TOKEN="):
        value = line.split("=", 1)[1]
        break
if (value is None or len(value) < 32 or value == "REPLACE_WITH_RANDOM_TOKEN"
        or any(character.isspace() for character in value)):
    raise SystemExit(1)
print(value)
PY
}

ORCHESTRA_MCP_HTTP_TOKEN="$(read_token)" \
  || { echo "gateway environment does not contain a valid token" >&2; exit 1; }
export ORCHESTRA_MCP_HTTP_TOKEN

command -v systemctl >/dev/null || { echo "systemctl is required" >&2; exit 1; }
command -v ss >/dev/null || { echo "ss is required" >&2; exit 1; }
systemctl is-active --quiet orchestra-mcp-http.service
LISTENERS="$(ss -ltnH)" || { echo "could not inspect listening sockets" >&2; exit 1; }
LOCAL_ADDRESSES="$(awk '{print $4}' <<<"$LISTENERS")"
grep -qx '127\.0\.0\.1:8090' <<<"$LOCAL_ADDRESSES" \
  || { echo "gateway is not listening on 127.0.0.1:8090" >&2; exit 1; }
if grep -E '(^|:)8090$' <<<"$LOCAL_ADDRESSES" | grep -vqx '127\.0\.0\.1:8090'; then
  echo "gateway is listening on a non-loopback address" >&2
  exit 1
fi
SOCKET_METADATA="$(stat -c '%u %g %a' /run/orchestra/omcp.sock 2>/dev/null \
  || stat -f '%u %g %Lp' /run/orchestra/omcp.sock)"
read -r SOCKET_UID SOCKET_GID SOCKET_MODE <<<"$SOCKET_METADATA"
[[ "$SOCKET_UID" == "$OMCP_UID" && "$SOCKET_GID" == "$EXPECTED_GID" && "$SOCKET_MODE" == "660" ]] \
  || { echo "coordinator socket identity/mode does not match policy" >&2; exit 1; }
sudo -u "$MCP_HTTP_USER" /usr/bin/test -r /run/orchestra/omcp.sock
sudo -u "$MCP_HTTP_USER" /usr/bin/test -w /run/orchestra/omcp.sock
python3 - <<'PY'
import http.client, json, os
token=os.environ['ORCHESTRA_MCP_HTTP_TOKEN']; headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
def call(method,path,body=None,headers=None):
 c=http.client.HTTPConnection('127.0.0.1',8090,timeout=10); c.request(method,path,body,headers or {}); r=c.getresponse(); data=r.read(); h=dict(r.getheaders()); c.close(); return r.status,data,h
assert call('GET','/healthz')[0]==200
assert call('GET','/readyz')[0]==401
assert call('GET','/readyz',headers=headers)[0]==200
assert call('POST','/mcp',b'{}')[0]==401
assert call('POST','/mcp',b'{}',{'Authorization':'Bearer wrong-token','Content-Type':'application/json'})[0]==401
def cycle():
 s,b,h=call('POST','/mcp',json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}}).encode(),headers)
 assert s==200; sid=h['Mcp-Session-Id']; hh={**headers,'Mcp-Session-Id':sid}
 assert call('POST','/mcp',json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'}).encode(),hh)[0]==200
 for i,m in enumerate(('tools/list','tools/call'),2):
  p={'jsonrpc':'2.0','id':i,'method':m};
  if m=='tools/call': p['params']={'name':'bridge_status','arguments':{}}
  assert call('POST','/mcp',json.dumps(p).encode(),hh)[0]==200
 assert call('DELETE','/mcp',headers=hh)[0]==204
for _ in range(20): cycle()
print('MCP HTTP verification passed: 20 authenticated cycles')
PY
