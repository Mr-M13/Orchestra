from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from omcp import __version__
from omcp.controller import AuthorizationError, Config, TaskController
from omcp.local_control import LocalApprovalServer


PROTOCOL_VERSION = "2025-06-18"
MAX_MCP_LINE_BYTES = 4 * 1024 * 1024


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "object", "properties": properties, "additionalProperties": False,
    }
    if required:
        value["required"] = required
    return value


TOOLS = [
    {
        "name": "bridge_status",
        "description": "Check OMCP health and task counts without changing task state.",
        "inputSchema": _object({}),
    },
    {
        "name": "submit_task",
        "description": "Durably submit a worker task for an explicit repository profile. request_id makes retries idempotent.",
        "inputSchema": _object({
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "instructions": {"type": "string", "minLength": 1},
            "worker": {"type": "string", "minLength": 1},
            "repository_profile": {"type": "string", "minLength": 1, "maxLength": 64},
            "consequence_class": {
                "type": "string",
                "enum": [
                    "routine_development", "production_deployment", "destructive_operation",
                    "security_change", "unclassified",
                ],
                "default": "unclassified",
                "description": "Declared consequence class. In required mode only owner-configured routine_development scopes may use standing authorization; disabled mode automatically authorizes eligible routine_development only. Protected classes and unclassified work still require explicit approval.",
            },
        }, ["request_id", "title", "instructions", "worker", "repository_profile"]),
    },
    {
        "name": "submit_host_operation",
        "description": (
            "Request one owner-enabled, built-in host operation against an exact local commit. "
            "The caller cannot provide a command or arguments; every request requires local approval."
        ),
        "inputSchema": _object({
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "operation_id": {"type": "string", "minLength": 1, "maxLength": 64},
            "target_commit": {
                "type": "string", "pattern": "^[0-9a-f]{40}$",
                "description": "Full immutable commit SHA already present in the configured repository.",
            },
        }, ["request_id", "operation_id", "target_commit"]),
    },
    {
        "name": "task_status",
        "description": "Get the current durable status of one task.",
        "inputSchema": _object({"task_id": {"type": "string"}}, ["task_id"]),
    },
    {
        "name": "list_tasks",
        "description": "List recent tasks, optionally filtered by exact status.",
        "inputSchema": _object({
            "status": {"type": ["string", "null"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        }),
    },
    {
        "name": "task_result",
        "description": "Retrieve a bounded final worker result for a terminal task.",
        "inputSchema": _object({"task_id": {"type": "string"}}, ["task_id"]),
    },
    {
        "name": "worker_availability",
        "description": "List configured workers and current scheduler capacity.",
        "inputSchema": _object({}),
    },
    {
        "name": "cancel_task",
        "description": "Cancel a queued task or request termination of a running task.",
        "inputSchema": _object({
            "task_id": {"type": "string"},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        }, ["task_id", "reason"]),
    },
    {
        "name": "list_approvals",
        "description": "List tasks currently waiting for an explicit approval decision.",
        "inputSchema": _object({}),
    },
]


class MCPServer:
    def __init__(self, controller: TaskController):
        self.controller = controller
        self.stopping = False
        self.stop_event = threading.Event()
        self.controller_lock = threading.RLock()
        self.calls: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "bridge_status": lambda _: controller.bridge_status(),
            "submit_task": lambda value: controller.submit_task(
                value.get("request_id"), value.get("title"), value.get("instructions"),
                value.get("worker"), True, value.get("repository_profile"),
                value.get("consequence_class", "unclassified"),
            ),
            "submit_host_operation": lambda value: controller.submit_host_operation(
                value.get("request_id"), value.get("operation_id"), value.get("target_commit"),
            ),
            "task_status": lambda value: controller.task_status(value.get("task_id")),
            "list_tasks": lambda value: controller.list_tasks(value.get("status"), value.get("limit", 100)),
            "task_result": lambda value: controller.task_result(value.get("task_id")),
            "worker_availability": lambda _: controller.worker_availability(),
            "cancel_task": lambda value: controller.cancel_task(value.get("task_id"), value.get("reason")),
            "list_approvals": lambda _: controller.list_approvals(),
        }

    @staticmethod
    def _result(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": code, "message": message}}

    def handle(self, request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self._error(request.get("id") if isinstance(request, dict) else None, -32600, "Invalid Request")
        request_id = request.get("id")
        method = request["method"]
        if "id" not in request:
            if method == "notifications/initialized":
                return None
            return None
        if method == "initialize":
            return self._result(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "orchestra-omcp", "version": __version__},
                "instructions": "Submit bounded implementation tasks; Orchestra schedules within owner-configured parallel capacity and repository conflict rules.",
            })
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method != "tools/call":
            return self._error(request_id, -32601, "Method not found")
        params = request.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return self._error(request_id, -32602, "Invalid params")
        name = params["name"]
        arguments = params.get("arguments", {})
        if name not in self.calls or not isinstance(arguments, dict):
            return self._error(request_id, -32602, "Unknown tool or invalid arguments")
        try:
            with self.controller_lock:
                value = self.calls[name](arguments)
            return self._result(request_id, {
                "content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}],
                "structuredContent": value,
                "isError": False,
            })
        except (AuthorizationError, KeyError, ValueError) as exc:
            self.controller.audit_tool_failure(name, exc)
            return self._result(request_id, {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })
        except Exception as exc:
            self.controller.audit_tool_failure(name, exc)
            return self._result(request_id, {
                "content": [{"type": "text", "text": f"Internal error ({type(exc).__name__})"}],
                "isError": True,
            })

    def _start_auxiliary(self) -> tuple[LocalApprovalServer, threading.Thread]:
        local_approval = LocalApprovalServer(self.controller, self.controller_lock)
        local_approval.start()
        ticker = threading.Thread(target=self._tick_loop, name="omcp-reconciler", daemon=True)
        ticker.start()
        return local_approval, ticker

    def _stop_auxiliary(self, local_approval: LocalApprovalServer,
                        ticker: threading.Thread) -> None:
        self.stop_event.set()
        ticker.join(timeout=2)
        local_approval.stop()

    def _stdio_response(self, line: str) -> dict[str, Any] | None:
        try:
            return self.handle(json.loads(line))
        except json.JSONDecodeError:
            return self._error(None, -32700, "Parse error")

    def run(self) -> int:
        local_approval, ticker = self._start_auxiliary()
        try:
            for line in sys.stdin:
                if self.stopping:
                    break
                response = self._stdio_response(line)
                if response is not None:
                    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                    sys.stdout.flush()
            return 0
        finally:
            self._stop_auxiliary(local_approval, ticker)

    def run_socket(self, socket_path: Path) -> int:
        """Serve JSON-RPC over an owner-only Unix socket.

        The tunnel client owns a small stdio proxy, while this process remains
        the persistent coordinator.  This keeps the scheduler, approval socket
        and task state alive across individual tunnel connections.
        """
        socket_path = socket_path.expanduser().resolve()
        socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(socket_path.parent, 0o700)
        socket_path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        listener.listen(8)
        listener.settimeout(0.5)
        local_approval, ticker = self._start_auxiliary()
        connections: list[threading.Thread] = []
        try:
            while not self.stop_event.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    continue
                worker = threading.Thread(
                    target=self._serve_connection, args=(connection,),
                    name="omcp-mcp-connection", daemon=True,
                )
                worker.start()
                connections.append(worker)
                connections = [item for item in connections if item.is_alive()]
            return 0
        finally:
            self._stop_auxiliary(local_approval, ticker)
            listener.close()
            socket_path.unlink(missing_ok=True)
            for worker in connections:
                worker.join(timeout=1)

    def _serve_connection(self, connection: socket.socket) -> None:
        connection.settimeout(1.0)
        buffer = b""
        with connection:
            while not self.stop_event.is_set():
                try:
                    chunk = connection.recv(65_536)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > MAX_MCP_LINE_BYTES:
                    self._send_connection(connection, self._error(
                        None, -32600, "Request too large",
                    ))
                    return
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    try:
                        line = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self._send_connection(connection, self._error(
                            None, -32700, "Parse error",
                        ))
                        continue
                    response = self._stdio_response(line)
                    if response is not None:
                        self._send_connection(connection, response)

    @staticmethod
    def _send_connection(connection: socket.socket, response: dict[str, Any]) -> None:
        try:
            connection.sendall(
                json.dumps(response, separators=(",", ":")).encode() + b"\n"
            )
        except OSError:
            return

    def _tick_loop(self) -> None:
        while not self.stop_event.wait(1):
            try:
                with self.controller_lock:
                    self.controller.tick()
            except Exception as exc:
                try:
                    self.controller.audit_tool_failure("service.tick", exc)
                except Exception:
                    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orchestra MCP task-control server")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--socket", type=Path,
        help="serve JSON-RPC on an owner-only Unix socket instead of stdio",
    )
    args = parser.parse_args(argv)
    config = Config.load(args.config)
    if os.environ.get("OMCP_TUNNEL_AUTHENTICATED") != "1":
        raise PermissionError("an authenticated tunnel is required before OMCP starts")
    caller = os.environ.get("OMCP_AUTHENTICATED_CALLER")
    if not caller or caller != config.principal.identity:
        raise PermissionError("authenticated tunnel caller does not match configured principal")
    controller = TaskController(config)
    server = MCPServer(controller)
    def stop(*_: Any) -> None:
        server.stopping = True
        server.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if args.socket is not None:
            return server.run_socket(args.socket)
        return server.run()
    finally:
        controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
