import json, socket, tempfile, threading, unittest, http.client
from pathlib import Path
from omcp.http_gateway import Gateway, make_handler

class GatewayTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/"mcp.sock"; self.s=socket.socket(socket.AF_UNIX); self.s.bind(str(self.path)); self.path.chmod(0o600); self.s.listen(); self.stop=False
  def serve():
   while not self.stop:
    try: c,_=self.s.accept()
    except OSError: return
    def client(c):
     with c:
      f=c.makefile("rb")
      for line in f:
       r=json.loads(line)
       if "id" in r:
        result={"ok":True}
        if r.get("method")=="initialize": result["protocolVersion"]="2025-06-18"
        c.sendall(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":result}).encode()+b"\n")
    threading.Thread(target=client,args=(c,),daemon=True).start()
  threading.Thread(target=serve,daemon=True).start(); self.g=Gateway(self.path,"secret")
 def tearDown(self): self.stop=True; self.s.close(); self.tmp.cleanup()
 def test_twenty_fresh_handshakes_and_reconnects(self):
  for _ in range(20):
   sid,res=self.g.rpc(None,json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}).encode())
   self.assertIn(b'"ok": true',res); self.g.rpc(sid,b'{"jsonrpc":"2.0","method":"notifications/initialized"}')
   _,res=self.g.rpc(sid,b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}'); self.assertIn(b'"ok": true',res); self.g.close(sid)
 def test_rejects_early_and_unsupported(self):
  with self.assertRaises(PermissionError): self.g.rpc(None,b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
  with self.assertRaises(ValueError): self.g.rpc(None,b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28"}}')
 def test_real_http_health_auth_and_lifecycle(self):
  from http.server import ThreadingHTTPServer
  h=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.g)); threading.Thread(target=h.serve_forever,daemon=True).start()
  def req(method,path,body=None,headers={}):
   c=http.client.HTTPConnection('127.0.0.1',h.server_port); c.request(method,path,body,headers); r=c.getresponse(); x=r.read(); heads=dict(r.getheaders()); c.close(); return r.status,x,heads
  self.assertEqual(req('GET','/healthz')[0],200); self.assertEqual(req('GET','/readyz')[0],200)
  self.assertEqual(req('POST','/mcp',b'{}')[0],401)
  headers={'Authorization':'Bearer secret','Content-Type':'application/json'}
  self.assertEqual(req('POST','/mcp',b'{bad',headers)[0],400)
  s,b,heads=req('POST','/mcp',b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}',headers); self.assertEqual(s,200)
  hh={**headers,'Mcp-Session-Id':heads['Mcp-Session-Id']}; self.assertEqual(req('POST','/mcp',b'{"jsonrpc":"2.0","method":"notifications/initialized"}',hh)[0],200)
  self.assertEqual(req('POST','/mcp',b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}',hh)[0],200); h.shutdown(); h.server_close()
