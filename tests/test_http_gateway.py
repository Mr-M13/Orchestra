from __future__ import annotations

import grp
import http.client
import json
import os
import pwd
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from omcp.controller import Config, Principal, TaskController, Worker
from omcp.http_gateway import (
    MAX_BODY_BYTES, Gateway, GatewayError, GatewayServer, SessionLimitError,
    make_handler,
)
from omcp.mcp_proxy import (
    _validate_socket_path, validate_gateway_socket,
)
from omcp.server import MCPServer


TOKEN = "t" * 64


class FakeBackend:
    """Minimal newline-delimited OMCP backend for gateway lifecycle tests."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stop = threading.Event()
        self.connections: list[socket.socket] = []
        self.closed = 0
        self.lock = threading.Lock()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(32)
        self.listener.settimeout(0.1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            connection.settimeout(1)
            with self.lock:
                self.connections.append(connection)
            threading.Thread(
                target=self._handle, args=(connection,), daemon=True,
            ).start()

    def _handle(self, connection: socket.socket) -> None:
        buffer = b""
        try:
            with connection:
                while not self.stop.is_set():
                    try:
                        data = connection.recv(65536)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not data:
                        break
                    buffer += data
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        request = json.loads(line)
                        method = request.get("method")
                        if method == "notifications/initialized":
                            continue
                        result = {"ok": True}
                        if method == "tools/list":
                            result = {"tools": [{"name": "bridge_status"}]}
                        elif method == "tools/call":
                            result = {"structuredContent": {"service": "omcp"}}
                        if method == "initialize":
                            result = {"protocolVersion": "2025-06-18"}
                        response = {
                            "jsonrpc": "2.0", "id": request.get("id"),
                            "result": result,
                        }
                        connection.sendall(
                            json.dumps(response, separators=(",", ":")).encode() + b"\n"
                        )
        except (OSError, ValueError):
            pass
        with self.lock:
            self.closed += 1

    def drop_connections(self) -> None:
        with self.lock:
            connections = list(self.connections)
            self.connections.clear()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def close(self) -> None:
        self.stop.set()
        self.listener.close()
        self.drop_connections()
        self.thread.join(timeout=2)


class GatewayTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "omcp.sock"
        self.backend = FakeBackend(self.path)
        self.path.chmod(0o600)
        self.gateway = Gateway(self.path, TOKEN)

    def tearDown(self) -> None:
        self.gateway.shutdown()
        self.backend.close()
        self.temp.cleanup()

    def initialize(self, gateway: Gateway | None = None):
        gateway = gateway or self.gateway
        return gateway.rpc(
            None,
            json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }).encode(),
        )

    def ready_session(self, gateway: Gateway | None = None) -> str:
        gateway = gateway or self.gateway
        session, _ = self.initialize(gateway)
        gateway.rpc(
            session,
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
        )
        return session


class GatewayLifecycleTests(GatewayTestBase):
    def test_twenty_fresh_handshakes_and_reconnects(self):
        for _ in range(20):
            session = self.ready_session()
            _, response = self.gateway.rpc(
                session, b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
            )
            self.assertIn(b'"tools"', response)
            self.gateway.close(session)
        self.assertEqual(self.gateway.session_count, 0)

    def test_initialize_and_session_lifecycle_validation(self):
        with self.assertRaises(GatewayError):
            self.gateway.rpc(None, b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
        with self.assertRaises(GatewayError):
            self.gateway.rpc(None, b'[]')
        with self.assertRaises(GatewayError):
            self.gateway.rpc(None, b'{"jsonrpc":"2.0","method":"initialize"}')
        with self.assertRaises(GatewayError):
            self.gateway.rpc(None, json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2026-07-28"},
            }).encode())

        session, _ = self.initialize()
        with self.assertRaises(GatewayError):
            self.gateway.rpc(session, json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }).encode())
        with self.assertRaises(GatewayError):
            self.gateway.rpc(
                session, b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
            )
        with self.assertRaises(GatewayError):
            self.gateway.rpc(
                session,
                b'{"jsonrpc":"2.0","id":3,"method":"notifications/initialized"}',
            )
        with self.assertRaises(GatewayError):
            self.gateway.rpc(
                session,
                b'{"jsonrpc":"2.0","id":null,"method":"notifications/initialized"}',
            )
        self.gateway.rpc(
            session,
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
        )
        with self.assertRaises(GatewayError):
            self.gateway.rpc(
                session,
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
            )
        with self.assertRaises(GatewayError):
            self.gateway.rpc(
                session,
                b'{"jsonrpc":"2.0","method":"notifications/cancelled"}',
            )

    def test_canonicalization_cannot_expand_past_backend_line_limit(self):
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {"x": "😀" * 8},
        }).encode()
        self.assertLessEqual(len(payload), 200)
        with patch("omcp.http_gateway.MAX_BODY_BYTES", 200), \
             patch("omcp.http_gateway.MAX_MCP_LINE_BYTES", 100):
            with self.assertRaises(GatewayError) as raised:
                self.gateway.rpc(None, payload)
        self.assertEqual(raised.exception.status, 413)

    def test_trailing_newline_does_not_desynchronise_backend_framing(self):
        session = self.ready_session()
        first = self.gateway.rpc(
            session, b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n\n',
        )
        second = self.gateway.rpc(
            session, b'{"jsonrpc":"2.0","id":3,"method":"tools/list"}',
        )
        self.assertEqual(json.loads(first[1])["id"], 2)
        self.assertEqual(json.loads(second[1])["id"], 3)

    def test_session_limit_is_bounded_and_released_by_close(self):
        gateway = Gateway(self.path, TOKEN, max_sessions=2)
        try:
            first = self.ready_session(gateway)
            second = self.ready_session(gateway)
            with self.assertRaises(SessionLimitError):
                self.initialize(gateway)
            gateway.close(first)
            third = self.ready_session(gateway)
            self.assertEqual(gateway.session_count, 2)
            self.assertNotEqual(second, third)
        finally:
            gateway.shutdown()

    def test_stale_sessions_expire_and_close_backend_sockets(self):
        gateway = Gateway(self.path, TOKEN, session_ttl=1)
        try:
            session = self.ready_session(gateway)
            self.assertEqual(gateway.session_count, 1)
            self.assertEqual(
                gateway._expire(now=time.monotonic() + 2), 1,
            )
            self.assertEqual(gateway.session_count, 0)
            with self.assertRaises(GatewayError):
                gateway.rpc(session, b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
            for _ in range(50):
                if self.backend.closed:
                    break
                time.sleep(0.02)
            self.assertGreaterEqual(self.backend.closed, 1)
        finally:
            gateway.shutdown()

    def test_background_sweeper_expires_idle_sessions(self):
        gateway = Gateway(
            self.path, TOKEN, session_ttl=1, sweep_interval=1,
        )
        try:
            gateway.start()
            self.ready_session(gateway)
            self.assertEqual(gateway.session_count, 1)
            deadline = time.monotonic() + 4
            while gateway.session_count and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(gateway.session_count, 0)
        finally:
            gateway.shutdown()

    def test_backend_failure_drops_session(self):
        session = self.ready_session()
        self.backend.drop_connections()
        with self.assertRaises(GatewayError) as raised:
            self.gateway.rpc(
                session, b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
            )
        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(self.gateway.session_count, 0)

    def test_repeated_close_releases_the_session_slot_once(self):
        gateway = Gateway(self.path, TOKEN, max_sessions=1)
        try:
            session = self.ready_session(gateway)
            with gateway._lock:
                state = gateway.sessions.pop(session)
            for _ in range(50):
                gateway._close_state(state)
            self.ready_session(gateway)
            with self.assertRaises(SessionLimitError):
                self.initialize(gateway)
        finally:
            gateway.shutdown()

    def test_shutdown_closes_all_backend_sockets(self):
        self.ready_session()
        self.gateway.start()
        self.gateway.shutdown()
        self.assertEqual(self.gateway.session_count, 0)
        for _ in range(50):
            if self.backend.closed:
                break
            time.sleep(0.02)
        self.assertGreaterEqual(self.backend.closed, 1)


class GatewaySocketPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "omcp.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen(1)

    def tearDown(self):
        self.listener.close()
        self.temp.cleanup()

    def test_tunnel_socket_validation_accepts_only_configured_group_form(self):
        self.path.chmod(0o600)
        _validate_socket_path(self.path)
        self.path.chmod(0o660)
        group = grp.getgrgid(os.getgid()).gr_name
        _validate_socket_path(self.path, expected_group=group)
        with self.assertRaises(PermissionError):
            _validate_socket_path(self.path)
        self.path.chmod(0o604)
        with self.assertRaises(PermissionError):
            _validate_socket_path(self.path, expected_group=group)
        self.path.chmod(0o666)
        with self.assertRaises(PermissionError):
            _validate_socket_path(self.path, expected_group=group)

    def test_gateway_accepts_only_exact_owner_group_and_mode(self):
        owner = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        self.path.chmod(0o660)
        validate_gateway_socket(self.path, owner, group)
        self.path.chmod(0o600)
        with self.assertRaises(PermissionError):
            validate_gateway_socket(self.path, owner, group)
        self.path.chmod(0o666)
        with self.assertRaises(PermissionError):
            validate_gateway_socket(self.path, owner, group)
        self.path.chmod(0o660)
        with patch("omcp.mcp_proxy.pwd.getpwnam") as lookup_user:
            lookup_user.return_value.pw_uid = os.getuid() + 1
            with self.assertRaises(PermissionError):
                validate_gateway_socket(self.path, owner, group)

    def test_group_shared_socket_rejects_writable_parent_and_non_socket(self):
        owner = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        self.path.chmod(0o660)
        self.root.chmod(0o777)
        with self.assertRaises(PermissionError):
            validate_gateway_socket(self.path, owner, group)
        self.root.chmod(0o700)
        self.path.unlink()
        self.path.write_text("not a socket")
        with self.assertRaises(ConnectionError):
            validate_gateway_socket(self.path, owner, group)


class GatewayHTTPTests(GatewayTestBase):
    def setUp(self):
        super().setUp()
        self.server = GatewayServer(("127.0.0.1", 0), make_handler(self.gateway))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5,
        )
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        data = response.read()
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, data, response_headers

    @property
    def headers(self):
        return {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}

    def start_session(self):
        status, _, response_headers = self.request(
            "POST", "/mcp",
            json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }),
            self.headers,
        )
        self.assertEqual(status, 200)
        return response_headers["Mcp-Session-Id"]

    def test_health_auth_and_malformed_requests_are_bounded(self):
        self.assertEqual(self.request("GET", "/healthz")[0], 200)
        self.assertEqual(self.request("GET", "/readyz")[0], 401)
        self.assertEqual(self.request("GET", "/readyz", headers=self.headers)[0], 200)
        self.path.chmod(0o666)
        self.assertEqual(self.request("GET", "/readyz", headers=self.headers)[0], 503)
        self.path.chmod(0o600)
        self.assertEqual(self.request("GET", "/mcp")[0], 404)
        self.assertEqual(self.request("POST", "/mcp", b"{}")[0], 401)
        self.assertEqual(self.request("POST", "/mcp", b"{}", {
            "Authorization": "Bearer wrong",
        })[0], 401)
        self.assertEqual(self.request("POST", "/mcp", b"{}", {
            "Authorization": "Bearer",
        })[0], 401)
        self.assertEqual(self.request("POST", "/mcp", b"{bad", self.headers)[0], 400)
        self.assertEqual(self.request("POST", "/mcp", b"[]", self.headers)[0], 400)
        self.assertEqual(self.request(
            "POST", "/mcp", b'{"jsonrpc":"2.0"}', self.headers,
        )[0], 400)
        self.assertEqual(self.request(
            "POST", "/mcp", b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
            self.headers,
        )[0], 400)
        self.assertEqual(self.request(
            "POST", "/mcp",
            json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2026-07-28"},
            }),
            self.headers,
        )[0], 400)
        self.assertEqual(self.request(
            "POST", "/mcp", b"{}", {**self.headers, "Mcp-Session-Id": "A" * 32},
        )[0], 400)
        self.assertEqual(self.request(
            "POST", "/mcp", b"{}", {**self.headers, "Transfer-Encoding": "chunked"},
        )[0], 400)
        self.assertEqual(self.request(
            "POST", "/mcp", b"{}", {**self.headers, "Transfer-Encoding": ""},
        )[0], 400)
        self.assertEqual(self.request(
            "POST", "/mcp", b"{}", {**self.headers, "Content-Length": "+2"},
        )[0], 400)
        self.assertEqual(self.request(
            "POST", "/mcp", b"",
            {**self.headers, "Content-Length": str(MAX_BODY_BYTES + 1)},
        )[0], 413)

    def test_auth_failure_is_audited_without_logging_the_token(self):
        with self.assertLogs("orchestra.mcp.http", level="WARNING") as captured:
            self.assertEqual(self.request("POST", "/mcp", b"{}")[0], 401)
        output = "\n".join(captured.output)
        self.assertIn("auth_failure", output)
        self.assertNotIn(TOKEN, output)

    def test_absolute_request_deadline_closes_a_slow_header_connection(self):
        server = GatewayServer(
            ("127.0.0.1", 0), make_handler(self.gateway, request_deadline=0.2),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = socket.create_connection(
            ("127.0.0.1", server.server_port), timeout=2,
        )
        try:
            connection.sendall(b"GET /healthz HTTP/1.0\r\nX-Slow: ")
            connection.settimeout(2)
            try:
                response = connection.recv(1024)
            except ConnectionResetError:
                response = b""
            self.assertEqual(response, b"")
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_http_concurrency_limit_fails_closed_with_503(self):
        self.server._request_slots = threading.BoundedSemaphore(1)
        self.server._request_slots.acquire()
        connection = socket.create_connection(
            ("127.0.0.1", self.server.server_port), timeout=5,
        )
        try:
            connection.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
            response = connection.recv(4096)
        finally:
            self.server._request_slots.release()
            connection.close()
        self.assertIn(b"503 Service Unavailable", response)

    def test_authorization_header_is_exact_and_case_sensitive(self):
        self.assertTrue(self.gateway.allowed("Bearer " + TOKEN))
        self.assertFalse(self.gateway.allowed("bearer " + TOKEN))
        self.assertFalse(self.gateway.allowed("Bearer  " + TOKEN))
        self.assertFalse(self.gateway.allowed(TOKEN))
        self.assertFalse(self.gateway.allowed("Bearer " + TOKEN + " "))
        self.assertFalse(self.gateway.allowed(None))

    def test_http_session_lifecycle_and_unknown_session(self):
        session = self.start_session()
        headers = {**self.headers, "Mcp-Session-Id": session}
        self.assertEqual(self.request(
            "POST", "/mcp",
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}', headers,
        )[0], 200)
        self.assertEqual(self.request(
            "POST", "/mcp", b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}', headers,
        )[0], 200)
        self.assertEqual(self.request(
            "POST", "/mcp", b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
            {**self.headers, "Mcp-Session-Id": "f" * 32},
        )[0], 400)
        self.assertEqual(self.request(
            "DELETE", "/mcp", None, headers,
        )[0], 204)
        self.assertEqual(self.request(
            "DELETE", "/mcp", None, headers,
        )[0], 204)
        self.assertEqual(self.gateway.session_count, 0)

    def test_http_session_exhaustion_returns_429(self):
        gateway = Gateway(self.path, TOKEN, max_sessions=1)
        server = GatewayServer(("127.0.0.1", 0), make_handler(gateway))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            first = self.request_with(server, "POST", "/mcp", json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }), self.headers)
            second = self.request_with(server, "POST", "/mcp", json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }), self.headers)
            self.assertEqual(first[0], 200)
            self.assertEqual(second[0], 429)
        finally:
            gateway.shutdown()
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_stale_http_session_is_rejected_after_background_expiry(self):
        session = self.start_session()
        self.gateway._expire(now=time.monotonic() + 10_000)
        status, body, _ = self.request(
            "POST", "/mcp", b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
            {**self.headers, "Mcp-Session-Id": session},
        )
        self.assertEqual(status, 400)
        self.assertIn(b"invalid_request", body)
        self.assertEqual(self.gateway.session_count, 0)

    def test_backend_failure_returns_502(self):
        session = self.start_session()
        headers = {**self.headers, "Mcp-Session-Id": session}
        self.request(
            "POST", "/mcp",
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}', headers,
        )
        self.backend.drop_connections()
        status, body, _ = self.request(
            "POST", "/mcp", b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}', headers,
        )
        self.assertEqual(status, 502)
        self.assertIn(b"backend_unavailable", body)

    def request_with(self, server, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=5,
        )
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        data = response.read()
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, data, response_headers


class GatewayIntegrationTests(unittest.TestCase):
    def test_http_gateway_to_real_omcp_coordinator_socket(self):
        temporary = tempfile.TemporaryDirectory()
        base = Path(temporary.name)
        root = base / "project"
        root.mkdir()
        permissions = frozenset({
            "status:read", "tasks:submit", "tasks:read", "results:read",
            "workers:read", "tasks:cancel", "approvals:read",
            "host_operations:submit",
        })
        config = Config(
            "owner/repo", root, base / "state", "origin/main",
            {"ina": Worker("ina", ("codex",))},
            Principal("chatgpt:test", permissions, "test-session"),
        )
        controller = TaskController(config, workspace_preparer=lambda *_: root)
        server = MCPServer(controller)
        socket_path = config.state_dir / "mcp.sock"
        socket_owner = pwd.getpwuid(os.getuid()).pw_name
        socket_group = grp.getgrgid(os.getgid()).gr_name
        server_thread = threading.Thread(
            target=server.run_socket, args=(socket_path, socket_group), daemon=True,
        )
        server_thread.start()
        gateway = None
        http_thread = None
        http_server = None
        try:
            deadline = time.monotonic() + 5
            while not socket_path.exists():
                if time.monotonic() > deadline:
                    self.fail("coordinator socket was not created")
                time.sleep(0.02)
            self.assertEqual(socket_path.stat().st_mode & 0o777, 0o660)
            self.assertEqual(socket_path.parent.stat().st_mode & 0o777, 0o710)
            gateway = Gateway(
                socket_path, TOKEN,
                socket_owner=socket_owner, socket_group=socket_group,
            )
            http_server = GatewayServer(("127.0.0.1", 0), make_handler(gateway))
            http_thread = threading.Thread(
                target=http_server.serve_forever, daemon=True,
            )
            http_thread.start()

            def request(method, path, body=None, headers=None):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", http_server.server_port, timeout=5,
                )
                connection.request(method, path, body, headers or {})
                response = connection.getresponse()
                data = response.read()
                response_headers = dict(response.getheaders())
                connection.close()
                return response.status, data, response_headers

            headers = {
                "Authorization": "Bearer " + TOKEN,
                "Content-Type": "application/json",
            }
            status, _, response_headers = request(
                "POST", "/mcp", json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                }), headers,
            )
            self.assertEqual(status, 200)
            session = response_headers["Mcp-Session-Id"]
            session_headers = {**headers, "Mcp-Session-Id": session}
            self.assertEqual(request(
                "POST", "/mcp",
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
                session_headers,
            )[0], 200)
            status, body, _ = request(
                "POST", "/mcp", b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
                session_headers,
            )
            self.assertEqual(status, 200)
            self.assertIn("tools", json.loads(body)["result"])
            status, body, _ = request(
                "POST", "/mcp",
                json.dumps({
                    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "bridge_status", "arguments": {}},
                }), session_headers,
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(body)["result"]["structuredContent"]["service"], "omcp",
            )
            self.assertEqual(request("DELETE", "/mcp", None, session_headers)[0], 204)
        finally:
            if gateway is not None:
                gateway.shutdown()
            if http_server is not None:
                http_server.shutdown()
                if http_thread is not None:
                    http_thread.join(timeout=2)
                http_server.server_close()
            server.stop_event.set()
            server_thread.join(timeout=5)
            controller.close()
            temporary.cleanup()


class GatewayTokenTests(unittest.TestCase):
    def test_weak_tokens_are_rejected_without_logging_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "omcp.sock"
            for token in ("", "short", "x" * 63 + " ", "x" * 39 + "\x01",
                          "x" * 63 + "\n"):
                with self.assertRaises(ValueError):
                    Gateway(path, token)
            Gateway(path, "x" * 40)
            Gateway(path, "x" * 40 + "._~+/=-")


if __name__ == "__main__":
    unittest.main()
