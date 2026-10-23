"""A narrow server-side client for the published OMCP socket interface."""
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any


class OMCPError(RuntimeError):
    """The dashboard-safe description of an OMCP communication failure."""


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
