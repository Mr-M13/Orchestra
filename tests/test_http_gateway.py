import json, socket, tempfile, threading, unittest
from pathlib import Path
from omcp.http_gateway import Gateway

class GatewayTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/"mcp.sock"; self.s=socket.socket(socket.AF_UNIX); self.s.bind(str(self.path)); self.s.listen(); self.stop=False
  def serve():
   while not self.stop:
    try: c,_=self.s.accept()
    except OSError: return
    def client(c):
     with c:
      f=c.makefile("rb")
      for line in f:
       r=json.loads(line)
       if "id" in r: c.sendall(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":{"ok":True}}).encode()+b"\n")
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
