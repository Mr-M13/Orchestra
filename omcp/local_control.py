"""Owner-only local control channel for interactive OMCP approval decisions."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
from pathlib import Path
from typing import Any


SOCKET_NAME = "approval.sock"
TOKEN_NAME = "approval.token"


def _read_message(connection: socket.socket, limit: int) -> dict[str, Any]:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(min(65536, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            raise ValueError("local approval message is too large")
        if b"\n" in chunk:
            break
    raw = b"".join(chunks)
    line, separator, trailing = raw.partition(b"\n")
    if not separator or trailing:
        raise ValueError("local approval message must be one JSON line")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("local approval message must be an object")
    return value


def _write_owner_only(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    temporary.replace(path)
    os.chmod(path, 0o600)


class LocalApprovalServer:
    """Serve approval review and resolution only on an owner-only Unix socket."""

    def __init__(self, controller: Any, controller_lock: threading.RLock):
        self.controller = controller
        self.controller_lock = controller_lock
        self.socket_path = controller.config.state_dir / SOCKET_NAME
        self.token_path = controller.config.state_dir / TOKEN_NAME
        self.token = secrets.token_hex(32)
        self.stop_event = threading.Event()
        self.listener: socket.socket | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.socket_path.unlink(missing_ok=True)
        _write_owner_only(self.token_path, (self.token + "\n").encode())
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        listener.listen(4)
        listener.settimeout(0.5)
        self.listener = listener
        self.thread = threading.Thread(
            target=self._run, name="omcp-local-approval", daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.listener is not None:
            self.listener.close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.socket_path.unlink(missing_ok=True)
        self.token_path.unlink(missing_ok=True)

    def _run(self) -> None:
        assert self.listener is not None
        while not self.stop_event.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stop_event.is_set():
                    break
                continue
            with connection:
                try:
                    request = _read_message(
                        connection, self.controller.config.max_task_bytes + 8192,
                    )
                    response = {"ok": True, "result": self._handle(request)}
                except Exception as exc:
                    response = {
                        "ok": False,
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    }
                connection.sendall(
                    json.dumps(response, separators=(",", ":")).encode() + b"\n"
                )

    def _handle(self, request: dict[str, Any]) -> dict[str, Any]:
        supplied = request.get("token")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied, self.token):
            raise PermissionError("invalid local approval capability")
        action = request.get("action")
        with self.controller_lock:
            if action == "review":
                return self.controller.local_approval_details(request.get("task_id"))
            if action == "details":
                return self.controller.local_task_details(request.get("task_id"))
            if action == "history":
                return self.controller.local_approval_history(request.get("limit", 100))
            if action == "resolve":
                return self.controller.resolve_approval(
                    request.get("task_id"), request.get("decision"),
                    request.get("reason"), request.get("approver_identity"),
                )
        raise ValueError("unsupported local approval action")


def approval_request(state_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Send one authenticated request to the active local OMCP coordinator."""
    token_path = state_dir / TOKEN_NAME
    socket_path = state_dir / SOCKET_NAME
    token = token_path.read_text().strip()
    if not token:
        raise PermissionError("local approval capability is unavailable")
    payload = {**request, "token": token}
    encoded = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(10)
    try:
        connection.connect(str(socket_path))
        connection.sendall(encoded)
        response = _read_message(connection, 1_048_576)
    finally:
        connection.close()
    if response.get("ok") is not True:
        error = response.get("error", {})
        raise RuntimeError(f"{error.get('type', 'Error')}: {error.get('message', 'request failed')}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("invalid local approval response")
    return result
