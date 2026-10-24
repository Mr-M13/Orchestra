"""Loopback-only authenticated HTTP façade for the private OMCP socket."""
from __future__ import annotations
import argparse, hmac, json, os, socket, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX = 4 * 1024 * 1024

class Gateway:
    def __init__(self, path: Path, token: str):
        if not token: raise ValueError("ORCHESTRA_MCP_HTTP_TOKEN is required")
        self.path, self.token, self.sessions, self.lock = path, token, {}, threading.Lock()
    def allowed(self, header: str | None) -> bool:
        return bool(header and header.startswith("Bearer ") and hmac.compare_digest(header[7:], self.token))
    def rpc(self, session: str | None, payload: bytes) -> tuple[str, bytes]:
        request = json.loads(payload); method = request.get("method")
        with self.lock:
            if method == "initialize":
                session = uuid.uuid4().hex
                self.sessions[session] = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sessions[session].connect(str(self.path))
            if not session or session not in self.sessions: raise PermissionError("MCP session is not initialized")
            conn = self.sessions[session]; conn.sendall(payload + b"\n")
            if "id" not in request: return session, b"{}"
            response = b""
            while not response.endswith(b"\n"): response += conn.recv(65536)
            return session, response.rstrip()

def make_handler(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def send(self, status: int, body: bytes=b"", session: str|None=None):
            self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
            if session: self.send_header("Mcp-Session-Id", session)
            self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if self.path == "/healthz": return self.send(200, b'{"status":"ok"}')
            if self.path == "/readyz":
                try:
                    s=socket.socket(socket.AF_UNIX); s.settimeout(1); s.connect(str(gateway.path)); s.close(); return self.send(200,b'{"ready":true}')
                except OSError: return self.send(503,b'{"ready":false}')
            self.send(404,b'{}')
        def do_POST(self):
            if self.path != "/mcp": return self.send(404,b'{}')
            if not gateway.allowed(self.headers.get("Authorization")): return self.send(401,b'{"error":"unauthorized"}')
            size=int(self.headers.get("Content-Length","0"))
            if size < 1 or size > MAX: return self.send(413,b'{"error":"invalid request size"}')
            try:
                session, result = gateway.rpc(self.headers.get("Mcp-Session-Id"), self.rfile.read(size)); self.send(200,result,session)
            except (ValueError, PermissionError, OSError, json.JSONDecodeError): self.send(400,b'{"error":"invalid MCP request"}')
    return Handler

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--socket",type=Path,required=True); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=8090); a=p.parse_args(argv)
    if a.host != "127.0.0.1": p.error("gateway must bind only to 127.0.0.1")
    GatewayServer=ThreadingHTTPServer((a.host,a.port),make_handler(Gateway(a.socket,os.environ.get("ORCHESTRA_MCP_HTTP_TOKEN", ""))))
    GatewayServer.serve_forever()
if __name__ == "__main__": main()
