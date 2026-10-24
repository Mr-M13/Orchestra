"""A narrow server-side client for the published OMCP socket interface."""
from __future__ import annotations

import json
import re
import socket
from pathlib import Path
from typing import Any


class OMCPError(RuntimeError):
    """The dashboard-safe description of an OMCP communication failure."""


class LocalApprovalClient:
    """Use only OMCP's owner-only local approval capability, server-side."""

    def __init__(self, state_dir: str, approver_identity: str) -> None:
        path = Path(state_dir)
        if not path.is_absolute():
            raise ValueError("approval state directory must be absolute")
        if not re.fullmatch(r"local:dashboard:[A-Za-z0-9_.-]{1,64}", approver_identity):
            raise ValueError("dashboard approver identity must be a local dashboard identity")
        self.state_dir = path
        self.approver_identity = approver_identity

    def resolve(self, task_id: str, decision: str, reason: str) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise OMCPError("invalid approval decision")
        try:
            from omcp.local_control import approval_request
            return approval_request(self.state_dir, {
                "action": "resolve", "task_id": task_id, "decision": decision,
                "reason": reason, "approver_identity": self.approver_identity,
            })
        except (OSError, RuntimeError, ValueError, PermissionError) as exc:
            raise OMCPError("local approval is unavailable") from exc


class OMCPClient:
    """Call only the dashboard's fixed, audited OMCP tool allowlist."""

    _PROTOCOL_VERSION = "2025-06-18"
    _CLIENT_INFO = {"name": "orchestra-dashboard", "version": "0.2.0"}

    _TOOLS = {
        "bridge_status", "worker_availability", "list_tasks", "task_snapshot",
        "task_status", "task_result", "list_approvals", "submit_task", "cancel_task",
    }

    def __init__(self, socket_path: str, timeout: float = 3.0) -> None:
        if not Path(socket_path).is_absolute():
            raise ValueError("OMCP socket path must be absolute")
        self.socket_path = socket_path
        self.timeout = timeout
        self._request_number = 0

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if tool not in self._TOOLS:
            raise OMCPError("operation is not available in the dashboard")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                self._initialize(connection)
                request_id = self._next_request_id()
                request = {
                    "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments or {}},
                }
                self._send(connection, request)
                response = self._receive_line(connection)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise OMCPError("Orchestra is unavailable") from exc
        if (not isinstance(response, dict) or response.get("jsonrpc") != "2.0"
                or response.get("id") != request_id):
            raise OMCPError("invalid response from Orchestra")
        result = response.get("result")
        if not isinstance(result, dict) or result.get("isError"):
            raise OMCPError("Orchestra rejected the request")
        content = result.get("structuredContent")
        if not isinstance(content, dict):
            raise OMCPError("invalid response from Orchestra")
        return content

    def _next_request_id(self) -> int:
        self._request_number += 1
        return self._request_number

    @staticmethod
    def _send(connection: socket.socket, request: dict[str, Any]) -> None:
        connection.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())

    def _initialize(self, connection: socket.socket) -> None:
        request_id = self._next_request_id()
        self._send(connection, {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": self._PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": self._CLIENT_INFO,
            },
        })
        response = self._receive_line(connection)
        if (not isinstance(response, dict) or response.get("jsonrpc") != "2.0"
                or response.get("id") != request_id):
            raise OMCPError("invalid response from Orchestra")
        result = response.get("result")
        if (not isinstance(result, dict)
                or result.get("protocolVersion") != self._PROTOCOL_VERSION):
            raise OMCPError("Orchestra initialization failed")
        self._send(connection, {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })

    @staticmethod
    def _receive_line(connection: socket.socket) -> dict[str, Any]:
        data = bytearray()
        while len(data) <= 1_048_576:
            part = connection.recv(65536)
            if not part:
                break
            data.extend(part)
            if b"\n" in part:
                line = bytes(data).split(b"\n", 1)[0]
                return json.loads(line.decode("utf-8"))
        raise OMCPError("invalid response from Orchestra")
