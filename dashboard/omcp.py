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

    _TOOLS = {
        "bridge_status", "worker_availability", "list_tasks", "task_status",
        "task_result", "list_approvals", "submit_task", "cancel_task",
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
        self._request_number += 1
        request = {
            "jsonrpc": "2.0", "id": self._request_number, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                connection.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
                response = self._receive_line(connection)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise OMCPError("Orchestra is unavailable") from exc
        if not isinstance(response, dict) or response.get("id") != self._request_number:
            raise OMCPError("invalid response from Orchestra")
        result = response.get("result")
        if not isinstance(result, dict) or result.get("isError"):
            raise OMCPError("Orchestra rejected the request")
        content = result.get("structuredContent")
        if not isinstance(content, dict):
            raise OMCPError("invalid response from Orchestra")
        return content

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
