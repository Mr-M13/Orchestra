"""Loopback-only authenticated HTTP façade for the private OMCP socket."""
from __future__ import annotations

import argparse
import hmac
import json
import logging
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

MAX_MCP_LINE_BYTES = 4 * 1024 * 1024
# Leave room for the newline that terminates the backend line.
MAX_BODY_BYTES = MAX_MCP_LINE_BYTES - 1
SESSION_TTL_SECONDS = 900
MAX_SESSIONS = 64
SWEEP_INTERVAL_SECONDS = 30
SOCKET_TIMEOUT_SECONDS = 10.0
HTTP_TIMEOUT_SECONDS = 15.0
HTTP_REQUEST_DEADLINE_SECONDS = 20.0
MAX_HTTP_CONCURRENCY = 64
PROTOCOL_VERSION = "2025-06-18"
SESSION_ID_RE = re.compile(r"[0-9a-f]{32}")
MIN_TOKEN_LENGTH = 32
TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/=-]{32,}")

LOGGER = logging.getLogger("orchestra.mcp.http")
LOGGER.addHandler(logging.NullHandler())


def audit(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a bounded, token-free lifecycle/security event."""
    LOGGER.log(level, json.dumps({"event": event, **fields}, sort_keys=True))


class GatewayError(Exception):
    status = 400
    code = "invalid_request"

    def __init__(self, message: str = "invalid MCP request") -> None:
        super().__init__(message)


class SessionLimitError(GatewayError):
    status = 429
    code = "session_limit"


class RequestTooLarge(GatewayError):
    status = 413
    code = "request_too_large"


class BackendUnavailable(GatewayError):
    status = 502
    code = "backend_unavailable"


@dataclass
class Session:
    socket: socket.socket
    last: float
    ready: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    close_lock: threading.Lock = field(default_factory=threading.Lock)
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
                or TOKEN_RE.fullmatch(token) is None):
            raise ValueError(
                "ORCHESTRA_MCP_HTTP_TOKEN must be at least 32 safe header characters"
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
            with state.lock:
                self._close_state(state)
        audit("gateway_shutdown", closed=len(sessions))

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
        # Forward one canonical line.  This strips trailing whitespace/newlines
        # that would otherwise desynchronise the backend's line framing.
        canonical = json.dumps(
            request, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        if len(canonical) + 1 > MAX_MCP_LINE_BYTES:
            raise RequestTooLarge()
        if method == "initialize":
            if session:
                raise GatewayError("MCP session already exists")
            return self._initialize(request, canonical)
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
                self._drop(session, state, "expired")
                raise GatewayError("MCP session expired")
            if method == "notifications/initialized":
                if "id" in request:
                    raise GatewayError("initialized notification must not have an id")
                if state.ready:
                    raise GatewayError("MCP session is already initialized")
                try:
                    self._send(state, canonical)
                except OSError as exc:
                    self._drop(session, state, "backend_failure")
                    raise BackendUnavailable() from exc
                state.ready = True
                state.last = now
                return session, b"{}"
            if request.get("id") is None:
                raise GatewayError("unsupported MCP notification")
            if not state.ready:
                raise GatewayError("MCP session is not initialized")
            try:
                response = self._relay(state, canonical, request.get("id"))
            except OSError as exc:
                self._drop(session, state, "backend_failure")
                raise BackendUnavailable() from exc
            state.last = time.monotonic()
            return session, response

    def close(self, session: str | None) -> None:
        if not isinstance(session, str):
            return
        with self._lock:
            state = self.sessions.pop(session, None)
            active = len(self.sessions)
        if state is not None:
            with state.lock:
                self._close_state(state)
            audit("session_closed", reason="client", active=active)

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
            audit("session_limit", logging.WARNING, active=self.session_count)
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
                active = len(self.sessions)
            connection = None
            audit("session_created", active=active)
            return session, response.rstrip()
        except (OSError, ValueError, json.JSONDecodeError, GatewayError) as exc:
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

    def _relay(self, state: Session, payload: bytes, request_id: Any) -> bytes:
        self._send(state, payload)
        response = self._read_line(state.socket)
        try:
            parsed = json.loads(response)
        except (ValueError, UnicodeDecodeError) as exc:
            raise OSError("backend returned invalid JSON") from exc
        if not isinstance(parsed, dict) or parsed.get("id") != request_id:
            raise OSError("backend response id mismatch")
        return response

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
            if len(response) > MAX_MCP_LINE_BYTES:
                raise OSError("backend response exceeds the size limit")
        return bytes(response).split(b"\n", 1)[0].rstrip()

    def _drop(self, session: str, state: Session, reason: str) -> None:
        with self._lock:
            removed = self.sessions.get(session) is state
            if removed:
                del self.sessions[session]
            active = len(self.sessions)
        if not removed:
            return
        self._close_state(state)
        audit("session_closed", reason=reason, active=active)

    def _close_state(self, state: Session) -> None:
        with state.close_lock:
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
                        self._close_state(state)
                finally:
                    state.lock.release()
        if expired:
            with self._lock:
                active = len(self.sessions)
            audit("session_expired", count=len(expired), active=active)
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


def make_handler(gateway: Gateway,
                 request_deadline: float = HTTP_REQUEST_DEADLINE_SECONDS):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Orchestra"
        sys_version = ""

        def log_message(self, *_args: Any) -> None:
            pass

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(HTTP_TIMEOUT_SECONDS)

        def handle_one_request(self) -> None:
            def abort_slow_request() -> None:
                audit("request_timeout", logging.WARNING)
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = threading.Timer(max(0.05, request_deadline), abort_slow_request)
            timer.daemon = True
            timer.start()
            try:
                super().handle_one_request()
            finally:
                timer.cancel()

        def send(self, status: int, body: bytes = b"",
                 session: str | None = None) -> None:
            try:
                self.send_response(status)
                if body:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                if session:
                    self.send_header("Mcp-Session-Id", session)
                self.end_headers()
                if body:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.close_connection = True

        def do_GET(self) -> None:
            if self.path == "/healthz":
                return self.send(200, b'{"status":"ok"}')
            if self.path == "/readyz":
                if not gateway.allowed(self.headers.get("Authorization")):
                    audit("auth_failure", logging.WARNING, status=401)
                    return self.send(401, b'{"error":"unauthorized"}')
                if gateway.ready():
                    return self.send(200, b'{"ready":true}')
                return self.send(503, b'{"ready":false}')
            self.send(404, b"{}")

        def do_POST(self) -> None:
            if self.path != "/mcp":
                return self.send(404, b"{}")
            if not gateway.allowed(self.headers.get("Authorization")):
                audit("auth_failure", logging.WARNING, status=401)
                return self.send(401, b'{"error":"unauthorized"}')
            if self.headers.get_all("Transfer-Encoding") is not None:
                audit("request_rejected", logging.WARNING, status=400,
                      code="invalid_transfer_encoding")
                return self.send(400, b'{"error":"invalid request size"}')
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
                audit("request_rejected", logging.WARNING,
                      status=400, code="invalid_content_length")
                return self.send(400, b'{"error":"invalid request size"}')
            size = int(lengths[0])
            if size < 1 or size > MAX_BODY_BYTES:
                audit("request_rejected", logging.WARNING,
                      status=413, code="invalid_request_size")
                return self.send(413, b'{"error":"invalid request size"}')
            session = self.headers.get("Mcp-Session-Id")
            if session is not None and not SESSION_ID_RE.fullmatch(session):
                audit("request_rejected", logging.WARNING,
                      status=400, code="invalid_session_id")
                return self.send(400, b'{"error":"invalid MCP session"}')
            body = self.rfile.read(size)
            if len(body) != size:
                audit("request_rejected", logging.WARNING,
                      status=400, code="incomplete_body")
                return self.send(400, b'{"error":"incomplete request body"}')
            try:
                session, result = gateway.rpc(session, body)
            except GatewayError as exc:
                audit("request_rejected", logging.WARNING,
                      status=exc.status, code=exc.code)
                return self.send(*_json_error(exc.status, exc.code))
            except (ValueError, json.JSONDecodeError):
                audit("request_rejected", logging.WARNING,
                      status=400, code="invalid_request")
                return self.send(400, b'{"error":"invalid MCP request"}')
            except Exception as exc:
                audit("internal_error", logging.ERROR,
                      error_type=type(exc).__name__)
                return self.send(500, b'{"error":"internal_error"}')
            self.send(200, result, session)

        def do_DELETE(self) -> None:
            if self.path != "/mcp":
                return self.send(404, b"{}")
            if not gateway.allowed(self.headers.get("Authorization")):
                audit("auth_failure", logging.WARNING, status=401, method="DELETE")
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
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    gateway.start()
    audit("gateway_started", host=args.host, port=args.port)
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
