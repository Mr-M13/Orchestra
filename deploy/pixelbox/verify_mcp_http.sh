#!/usr/bin/env bash
# Read-only live verifier. It never prints ORCHESTRA_MCP_HTTP_TOKEN.
set -euo pipefail
ENV_FILE="${ORCHESTRA_MCP_HTTP_ENV:-/etc/orchestra/mcp-http.env}"
[[ -r "$ENV_FILE" ]] || { echo "missing gateway environment" >&2; exit 1; }
set -a; source "$ENV_FILE"; set +a
[[ -n "${ORCHESTRA_MCP_HTTP_TOKEN:-}" ]] || { echo "missing gateway token" >&2; exit 1; }
systemctl is-active --quiet orchestra-mcp-http.service
ss -ltnH | awk '$4 == "127.0.0.1:8090" { found=1 } END { exit !found }'
! ss -ltnH | awk '$4 ~ /(^|:)8090$/ && $4 !~ /^127\.0\.0\.1:8090$/ { exit 1 }'
python3 - <<'PY'
import http.client, json, os
token=os.environ['ORCHESTRA_MCP_HTTP_TOKEN']; headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
def call(method,path,body=None,headers=None):
 c=http.client.HTTPConnection('127.0.0.1',8090,timeout=10); c.request(method,path,body,headers or {}); r=c.getresponse(); data=r.read(); h=dict(r.getheaders()); c.close(); return r.status,data,h
assert call('GET','/healthz')[0]==200 and call('GET','/readyz')[0]==200
assert call('POST','/mcp',b'{}')[0]==401
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
