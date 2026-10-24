"""Loopback-only authenticated HTTP façade for the private OMCP socket."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import signal
import socket
import socketserver
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from omcp.mcp_proxy import _validate_socket_path, connect_gateway

MAX_BODY_BYTES = 4 * 1024 * 1024
SESSION_TTL_SECONDS = 900
MAX_SESSIONS = 64
SWEEP_INTERVAL_SECONDS = 30
SOCKET_TIMEOUT_SECONDS = 10.0
HTTP_TIMEOUT_SECONDS = 15.0
MAX_HTTP_CONCURRENCY = 64
PROTOCOL_VERSION = "2025-06-18"
SESSION_ID_RE = re.compile(r"[0-9a-f]{32}")
MIN_TOKEN_LENGTH = 32


class GatewayError(Exception):
    status = 400
    code = "invalid_request"

    def __init__(self, message: str = "invalid MCP request") -> None:
        super().__init__(message)


class SessionLimitError(GatewayError):
    status = 429
    code = "session_limit"


class BackendUnavailable(GatewayError):
    status = 502
    code = "backend_unavailable"


@dataclass
class Session:
    socket: socket.socket
    last: float
    ready: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False


class Gateway:
    def __init__(
        self,
        path: Path,
        token: str,
        *,
        socket_owner: str | None = None,
        socket_group: str | None = None,
        session_ttl: int = SESSION_TTL_SECONDS,
        max_sessions: int = MAX_SESSIONS,
        sweep_interval: int = SWEEP_INTERVAL_SECONDS,
        socket_timeout: float = SOCKET_TIMEOUT_SECONDS,
    ) -> None:
        if (not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH
                or any(character.isspace() for character in token)):
            raise ValueError(
                "ORCHESTRA_MCP_HTTP_TOKEN must be at least 32 non-whitespace characters"
            )
        if bool(socket_owner) != bool(socket_group):
            raise ValueError("gateway socket owner and group must be configured together")
        if not isinstance(session_ttl, int) or session_ttl < 1:
            raise ValueError("session_ttl must be a positive integer")
        if not isinstance(max_sessions, int) or max_sessions < 1:
            raise ValueError("max_sessions must be a positive integer")
        if not isinstance(sweep_interval, int) or sweep_interval < 0:
            raise ValueError("sweep_interval must be a non-negative integer")
        if not isinstance(socket_timeout, (int, float)) or socket_timeout <= 0:
            raise ValueError("socket_timeout must be positive")
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("gateway socket path must be absolute")
        self.token = token
        self.socket_owner = socket_owner
        self.socket_group = socket_group
        self.session_ttl = session_ttl
        self.max_sessions = max_sessions
        self.sweep_interval = sweep_interval
        self.socket_timeout = float(socket_timeout)
        self.sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_sessions)
        self._stop = threading.Event()
        self._sweeper: threading.Thread | None = None

    def start(self) -> None:
        if self.sweep_interval == 0 or self._sweeper is not None:
            return
        self._sweeper = threading.Thread(
            target=self._sweep_loop, name="orchestra-mcp-http-sessions", daemon=True,
        )
        self._sweeper.start()

    def shutdown(self) -> None:
        self._stop.set()
        sweeper = self._sweeper
        if sweeper is not None:
            sweeper.join(timeout=2)
            self._sweeper = None
        with self._lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for state in sessions:
            self._close_state(state)

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self.sessions)

    def allowed(self, header: str | None) -> bool:
        if not isinstance(header, str):
            return False
        parts = header.split(" ")
        if len(parts) != 2 or parts[0] != "Bearer":
            return False
        try:
            return hmac.compare_digest(
                parts[1].encode("utf-8"), self.token.encode("utf-8"),
            )
        except (UnicodeEncodeError, ValueError):
            return False

    def ready(self) -> bool:
        try:
            connection = self._backend_socket()
        except (OSError, PermissionError, ValueError, KeyError):
            return False
        connection.close()
        return True

    def rpc(self, session: str | None, payload: bytes) -> tuple[str, bytes]:
        request, method = self._parse(payload)
        if method == "initialize":
            if session:
                raise GatewayError("MCP session already exists")
            return self._initialize(request, payload)
        if not session:
            raise GatewayError("MCP session is required")
        state = self._lookup(session)
        with state.lock:
            with self._lock:
                active = self.sessions.get(session)
            if state.closed or active is not state:
                raise GatewayError("MCP session is not initialized")
            now = time.monotonic()
            if now - state.last >= self.session_ttl:
                self._drop(session, state)
                raise GatewayError("MCP session expired")
            if method == "notifications/initialized":
                if request.get("id") is not None:
                    raise GatewayError("initialized notification must not have an id")
                if state.ready:
                    raise GatewayError("MCP session is already initialized")
                try:
                    self._send(state, payload)
                except OSError as exc:
                    self._drop(session, state)
                    raise BackendUnavailable() from exc
                state.ready = True
                state.last = now
                return session, b"{}"
            if request.get("id") is None:
                raise GatewayError("unsupported MCP notification")
            if not state.ready:
                raise GatewayError("MCP session is not initialized")
            try:
                response = self._relay(state, payload)
            except OSError as exc:
                self._drop(session, state)
                raise BackendUnavailable() from exc
            state.last = time.monotonic()
            return session, response

    def close(self, session: str | None) -> None:
        if not isinstance(session, str):
            return
        with self._lock:
            state = self.sessions.pop(session, None)
        if state is not None:
            self._close_state(state)

    def _parse(self, payload: bytes) -> tuple[dict[str, Any], str]:
        if not payload or len(payload) > MAX_BODY_BYTES:
            raise GatewayError("invalid request size")
        try:
            request = json.loads(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise GatewayError("malformed MCP request") from exc
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            raise GatewayError("malformed MCP request")
        method = request.get("method")
        if not isinstance(method, str) or not method:
            raise GatewayError("invalid MCP method")
        return request, method

    def _initialize(self, request: dict[str, Any], payload: bytes) -> tuple[str, bytes]:
        if request.get("id") is None:
            raise GatewayError("initialize requires an id")
        params = request.get("params")
        if not isinstance(params, dict) or params.get("protocolVersion") != PROTOCOL_VERSION:
            raise GatewayError("unsupported MCP protocol version")
        if not self._slots.acquire(blocking=False):
            raise SessionLimitError()
        connection: socket.socket | None = None
        try:
            connection = self._backend_socket()
            connection.sendall(payload + b"\n")
            response = self._read_line(connection)
            parsed = json.loads(response)
            if not isinstance(parsed, dict) or parsed.get("id") != request.get("id"):
                raise ValueError("invalid backend initialize response")
            result = parsed.get("result")
            if not isinstance(result, dict) or result.get("protocolVersion") != PROTOCOL_VERSION:
                raise ValueError("backend negotiated unsupported MCP protocol")
            session = uuid.uuid4().hex
            state = Session(connection, time.monotonic())
            with self._lock:
                if self._stop.is_set():
                    raise BackendUnavailable()
                self.sessions[session] = state
            connection = None
            return session, response.rstrip()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            if connection is not None:
                connection.close()
            self._slots.release()
            if isinstance(exc, GatewayError):
                raise
            raise BackendUnavailable() from exc

    def _lookup(self, session: str) -> Session:
        if not SESSION_ID_RE.fullmatch(session):
            raise GatewayError("invalid MCP session")
        with self._lock:
            state = self.sessions.get(session)
        if state is None:
            raise GatewayError("MCP session is not initialized")
        return state

    def _backend_socket(self) -> socket.socket:
        if self.socket_owner and self.socket_group:
            return connect_gateway(
                self.path, self.socket_owner, self.socket_group,
                timeout=self.socket_timeout,
            )
        _validate_socket_path(self.path)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.socket_timeout)
        try:
            connection.connect(str(self.path))
        except OSError:
            connection.close()
            raise
        return connection

    def _relay(self, state: Session, payload: bytes) -> bytes:
        self._send(state, payload)
        return self._read_line(state.socket)

    @staticmethod
    def _send(state: Session, payload: bytes) -> None:
        state.socket.sendall(payload + b"\n")

    @staticmethod
    def _read_line(connection: socket.socket) -> bytes:
        response = bytearray()
        while not response.endswith(b"\n"):
            block = connection.recv(65_536)
            if not block:
                raise OSError("backend closed the connection")
            response.extend(block)
            if len(response) > MAX_BODY_BYTES:
                raise OSError("backend response exceeds the size limit")
        return bytes(response).split(b"\n", 1)[0].rstrip()

    def _drop(self, session: str, state: Session) -> None:
        with self._lock:
            if self.sessions.get(session) is state:
                del self.sessions[session]
        self._close_state(state)

    def _close_state(self, state: Session) -> None:
        if state.closed:
            return
        state.closed = True
        try:
            state.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            state.socket.close()
        except OSError:
            pass
        try:
            self._slots.release()
        except ValueError:
            pass

    def _expire(self, now: float | None = None) -> int:
        threshold = time.monotonic() if now is None else now
        expired: list[Session] = []
        with self._lock:
            for session, state in list(self.sessions.items()):
                if state.closed or threshold - state.last < self.session_ttl:
                    continue
                if not state.lock.acquire(blocking=False):
                    continue
                try:
                    if (self.sessions.get(session) is state
                            and threshold - state.last >= self.session_ttl):
                        del self.sessions[session]
                        expired.append(state)
                finally:
                    state.lock.release()
        for state in expired:
            self._close_state(state)
        return len(expired)

    def _sweep_loop(self) -> None:
        while not self._stop.wait(self.sweep_interval):
            self._expire()


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_slots = threading.BoundedSemaphore(MAX_HTTP_CONCURRENCY)

    def server_bind(self) -> None:
        # HTTPServer resolves the bind address with getfqdn(); the service is
        # intentionally denied all external network traffic, so avoid a DNS
        # lookup that could stall startup or leak a resolver dependency.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def _json_error(status: int, code: str) -> tuple[int, bytes]:
    return status, json.dumps({"error": code}, separators=(",", ":")).encode()


def make_handler(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Orchestra"
        sys_version = ""

        def log_message(self, *_args: Any) -> None:
            pass

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(HTTP_TIMEOUT_SECONDS)

        def send(self, status: int, body: bytes = b"",
                 session: str | None = None) -> None:
            self.send_response(status)
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                return self.send(200, b'{"status":"ok"}')
            if self.path == "/readyz":
                if gateway.ready():
                    return self.send(200, b'{"ready":true}')
                return self.send(503, b'{"ready":false}')
            self.send(404, b"{}")

        def do_POST(self) -> None:
            if self.path != "/mcp":
                return self.send(404, b"{}")
            if not gateway.allowed(self.headers.get("Authorization")):
                return self.send(401, b'{"error":"unauthorized"}')
            if self.headers.get("Transfer-Encoding"):
                return self.send(400, b'{"error":"invalid request size"}')
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1:
                return self.send(400, b'{"error":"invalid request size"}')
            try:
                size = int(lengths[0])
            except ValueError:
                return self.send(400, b'{"error":"invalid request size"}')
            if size < 1 or size > MAX_BODY_BYTES:
                return self.send(413, b'{"error":"invalid request size"}')
            session = self.headers.get("Mcp-Session-Id")
            if session is not None and not SESSION_ID_RE.fullmatch(session):
                return self.send(400, b'{"error":"invalid MCP session"}')
            body = self.rfile.read(size)
            if len(body) != size:
                return self.send(400, b'{"error":"incomplete request body"}')
            try:
                session, result = gateway.rpc(session, body)
            except GatewayError as exc:
                return self.send(*_json_error(exc.status, exc.code))
            except (ValueError, json.JSONDecodeError):
                return self.send(400, b'{"error":"invalid MCP request"}')
            except Exception:
                return self.send(500, b'{"error":"internal_error"}')
            self.send(200, result, session)

        def do_DELETE(self) -> None:
            if self.path != "/mcp":
                return self.send(404, b"{}")
            if not gateway.allowed(self.headers.get("Authorization")):
                return self.send(401, b'{"error":"unauthorized"}')
            session = self.headers.get("Mcp-Session-Id")
            if not isinstance(session, str) or not SESSION_ID_RE.fullmatch(session):
                return self.send(400, b'{"error":"invalid MCP session"}')
            gateway.close(session)
            self.send(204)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orchestra loopback HTTP MCP gateway")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--socket-owner", default=os.environ.get("ORCHESTRA_OMCP_SOCKET_OWNER"))
    parser.add_argument("--socket-group", default=os.environ.get("ORCHESTRA_OMCP_SOCKET_GROUP"))
    parser.add_argument("--session-ttl", type=int, default=SESSION_TTL_SECONDS)
    parser.add_argument("--max-sessions", type=int, default=MAX_SESSIONS)
    parser.add_argument("--sweep-interval", type=int, default=SWEEP_INTERVAL_SECONDS)
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1":
        parser.error("gateway must bind only to 127.0.0.1")
    if not args.socket_owner or not args.socket_group:
        parser.error("gateway requires an explicit socket owner and group")
    token = os.environ.get("ORCHESTRA_MCP_HTTP_TOKEN", "")
    try:
        gateway = Gateway(
            args.socket, token,
            socket_owner=args.socket_owner, socket_group=args.socket_group,
            session_ttl=args.session_ttl, max_sessions=args.max_sessions,
            sweep_interval=args.sweep_interval,
        )
    except ValueError as exc:
        parser.error(str(exc))
    os.umask(0o077)
    gateway.start()
    server: GatewayServer | None = None
    worker: threading.Thread | None = None
    stop = threading.Event()

    def request_stop(*_args: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        server = GatewayServer((args.host, args.port), make_handler(gateway))
        worker = threading.Thread(
            target=server.serve_forever, name="orchestra-mcp-http-server", daemon=True,
        )
        worker.start()
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None and worker is not None:
            server.shutdown()
            worker.join(timeout=5)
        if server is not None:
            server.server_close()
        gateway.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
