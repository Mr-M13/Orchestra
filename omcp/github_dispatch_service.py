"""Credential-isolated, bounded GitHub Actions dispatch broker.

Only this process reads the GitHub token.  The coordinator talks to it over a
Unix socket and can request only the fixed operations implemented below; it
cannot select a repository, workflow, ref, SHA, URL, header, or credential.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omcp.github_actions import (
    DispatchIdentity, GitHubActionsClient, GitHubActionsError, RunState,
    _validate_dispatch_identity,
)


MAX_REQUEST_BYTES = 96 * 1024
MAX_RESPONSE_BYTES = 24 * 1024 * 1024


def _identity(value: object) -> DispatchIdentity:
    if not isinstance(value, dict):
        raise ValueError("dispatch identity must be an object")
    return _validate_dispatch_identity(DispatchIdentity(
        str(value.get("task_id", "")), str(value.get("request_id", "")),
        str(value.get("dispatch_id", "")), str(value.get("dispatched_at", "")),
    ))


def _identity_json(value: DispatchIdentity) -> dict[str, str]:
    return {
        "task_id": value.task_id, "request_id": value.request_id,
        "dispatch_id": value.dispatch_id, "dispatched_at": value.dispatched_at,
    }


def _run_json(value: RunState | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "run_id": value.run_id, "status": value.status,
        "conclusion": value.conclusion, "url": value.url,
    }


def _run(value: object) -> RunState | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise GitHubActionsError("dispatch broker returned an invalid run")
    run_id = value.get("run_id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise GitHubActionsError("dispatch broker returned an invalid run")
    return RunState(run_id, str(value.get("status", "unknown")),
                    value.get("conclusion"), value.get("url"))


def _receive(connection: socket.socket, maximum: int) -> dict[str, Any]:
    header = _receive_exact(connection, 4)
    size = struct.unpack("!I", header)[0]
    if size == 0 or size > maximum:
        raise ValueError("dispatch broker frame exceeded its bound")
    value = json.loads(_receive_exact(connection, size))
    if not isinstance(value, dict):
        raise ValueError("dispatch broker frame must be an object")
    return value


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        block = connection.recv(size - len(chunks))
        if not block:
            raise OSError("dispatch broker connection closed")
        chunks.extend(block)
    return bytes(chunks)


def _send(connection: socket.socket, value: dict[str, Any], maximum: int) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(payload) > maximum:
        raise ValueError("dispatch broker response exceeded its bound")
    connection.sendall(struct.pack("!I", len(payload)) + payload)


def ensure_dispatch_socket(path: Path, service_uid: int) -> None:
    """Fail unless path is a non-world-accessible socket of another UID."""
    if (not isinstance(service_uid, int) or isinstance(service_uid, bool)
            or service_uid < 0 or service_uid == os.geteuid()):
        raise PermissionError("dispatch broker must use a dedicated service UID")
    try:
        metadata = Path(path).lstat()
        parent = Path(path).parent.lstat()
    except OSError as exc:
        raise PermissionError("dispatch broker socket is unavailable") from exc
    if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != service_uid
            or metadata.st_mode & 0o007):
        raise PermissionError("dispatch broker socket ownership is invalid")
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != service_uid
            or parent.st_mode & 0o007):
        raise PermissionError("dispatch broker directory ownership is invalid")


@dataclass(frozen=True)
class ServiceConfig:
    repository: str
    workflow: str
    workflow_ref: str
    workflow_sha: str
    credential_file: Path
    service_uid: int
    coordinator_uid: int
    allowed_models: dict[str, frozenset[str]]

    @classmethod
    def load(cls, path: Path) -> "ServiceConfig":
        path = Path(path)
        metadata = path.lstat()
        parent_metadata = path.parent.lstat()
        if (stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077):
            raise PermissionError("dispatch broker config must be service-owner-only")
        if (stat.S_ISLNK(parent_metadata.st_mode)
                or not stat.S_ISDIR(parent_metadata.st_mode)
                or parent_metadata.st_uid != os.geteuid()
                or parent_metadata.st_mode & 0o077):
            raise PermissionError("dispatch broker config directory must be service-owner-only")
        raw = json.loads(path.read_text(encoding="utf-8"))
        service_uid = raw.get("service_uid")
        coordinator_uid = raw.get("coordinator_uid")
        if (not isinstance(service_uid, int) or isinstance(service_uid, bool)
                or service_uid != os.geteuid()):
            raise ValueError("service_uid must match the dispatch broker identity")
        if (not isinstance(coordinator_uid, int) or isinstance(coordinator_uid, bool)
                or coordinator_uid < 0 or coordinator_uid == service_uid):
            raise ValueError("coordinator_uid must be a different Unix identity")
        allowed = raw.get("allowed_models")
        if not isinstance(allowed, dict) or not allowed:
            raise ValueError("allowed_models must be a non-empty object")
        models: dict[str, frozenset[str]] = {}
        for agent, values in allowed.items():
            if (agent not in {"codex", "deepseek"} or not isinstance(values, list)
                    or not values or not all(isinstance(item, str) for item in values)):
                raise ValueError("allowed_models is invalid")
            models[agent] = frozenset(values)
        credential = Path(str(raw.get("credential_file", "")))
        if (not credential.is_absolute() or ".." in credential.parts):
            raise ValueError("credential_file must be an absolute normalized path")
        # The low-level client performs the strict token and parent checks.
        client = GitHubActionsClient(
            str(raw.get("github_repository", "")), str(raw.get("workflow", "")),
            str(raw.get("workflow_ref", "")), str(raw.get("workflow_sha", "")),
            credential, service_uid=service_uid,
        )
        return cls(client.repository, client.workflow, client.ref, client.head_sha,
                   credential, service_uid, coordinator_uid, models)

    def client(self) -> GitHubActionsClient:
        return GitHubActionsClient(
            self.repository, self.workflow, self.workflow_ref, self.workflow_sha,
            self.credential_file, service_uid=self.service_uid,
        )


class DispatchClient:
    """Coordinator-side client.  It never receives or reads a credential."""

    def __init__(self, socket_path: Path, service_uid: int, timeout: float = 30):
        self.socket_path = Path(socket_path)
        self.service_uid = service_uid
        self.timeout = timeout
        ensure_dispatch_socket(self.socket_path, service_uid)

    def _call(self, action: str, **params: Any) -> Any:
        ensure_dispatch_socket(self.socket_path, self.service_uid)
        request = {"version": 1, "action": action, **params}
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self.socket_path))
            _send(connection, request, MAX_REQUEST_BYTES)
            response = _receive(connection, MAX_RESPONSE_BYTES)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise GitHubActionsError(
                f"GitHub dispatch broker failed ({type(exc).__name__})"
            ) from None
        finally:
            connection.close()
        if response.get("ok") is not True:
            raise GitHubActionsError("GitHub dispatch broker rejected the request")
        return response.get("result")

    def dispatch(self, identity: DispatchIdentity, agent: str, model: str,
                 instructions: str) -> str:
        result = self._call("dispatch", identity=_identity_json(identity),
                            agent=agent, model=model, instructions=instructions)
        if not isinstance(result, str):
            raise GitHubActionsError("dispatch broker returned an invalid result")
        return result

    def find_run(self, identity: DispatchIdentity) -> RunState | None:
        return _run(self._call("find", identity=_identity_json(identity)))

    def status(self, run_id: int, identity: DispatchIdentity) -> RunState:
        value = _run(self._call("status", run_id=run_id,
                                identity=_identity_json(identity)))
        if value is None:
            raise GitHubActionsError("dispatch broker returned no run")
        return value

    def cancel(self, run_id: int, identity: DispatchIdentity) -> None:
        self._call("cancel", run_id=run_id, identity=_identity_json(identity))

    def download_artifact(self, run_id: int, name: str,
                          identity: DispatchIdentity) -> dict[str, bytes]:
        result = self._call("artifact", run_id=run_id, name=name,
                            identity=_identity_json(identity))
        if not isinstance(result, dict):
            raise GitHubActionsError("dispatch broker returned an invalid artifact")
        try:
            return {key: base64.b64decode(value, validate=True)
                    for key, value in result.items()}
        except (AttributeError, ValueError) as exc:
            raise GitHubActionsError("dispatch broker returned an invalid artifact") from exc

    def sleep(self, seconds: float) -> None:
        import time
        time.sleep(seconds)


class DispatchService:
    def __init__(self, config: ServiceConfig):
        self.config = config
        self.github = config.client()

    def handle(self, request: dict[str, Any]) -> Any:
        if request.get("version") != 1:
            raise ValueError("unsupported dispatch broker protocol")
        action = request.get("action")
        fields = {
            "dispatch": {"version", "action", "identity", "agent", "model", "instructions"},
            "find": {"version", "action", "identity"},
            "status": {"version", "action", "identity", "run_id"},
            "cancel": {"version", "action", "identity", "run_id"},
            "artifact": {"version", "action", "identity", "run_id", "name"},
        }.get(action)
        if fields is None or set(request) != fields:
            raise ValueError("dispatch broker request fields are invalid")
        identity = _identity(request.get("identity"))
        if action == "dispatch":
            agent, model = request.get("agent"), request.get("model")
            if (not isinstance(agent, str) or not isinstance(model, str)
                    or model not in self.config.allowed_models.get(agent, frozenset())):
                raise ValueError("agent and model are not allowlisted")
            instructions = request.get("instructions")
            if not isinstance(instructions, str):
                raise ValueError("instructions must be text")
            return self.github.dispatch(identity, agent, model, instructions)
        run_id = request.get("run_id")
        if action == "find":
            return _run_json(self.github.find_run(identity))
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            raise ValueError("run_id is invalid")
        # Bind every run operation to the full persisted dispatch identity.
        state = self.github.status(run_id, identity)
        if action == "status":
            return _run_json(state)
        if action == "cancel":
            self.github.cancel(run_id, identity)
            return None
        if action == "artifact":
            name = request.get("name")
            if name != "omcp-" + identity.task_id:
                raise ValueError("artifact is not bound to this task")
            files = self.github.download_artifact(run_id, name)
            return {key: base64.b64encode(value).decode("ascii")
                    for key, value in files.items()}
        raise ValueError("unsupported dispatch broker action")

    def serve(self, socket_path: Path) -> None:
        socket_path = Path(socket_path)
        parent = socket_path.parent
        metadata = parent.stat()
        if (metadata.st_uid != self.config.service_uid
                or metadata.st_mode & 0o007):
            raise PermissionError("dispatch broker runtime directory is not private")
        if socket_path.exists() or socket_path.is_symlink():
            raise FileExistsError("dispatch broker socket path already exists")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(socket_path))
            os.chmod(socket_path, 0o660)
            listener.listen(8)
            while True:
                connection, _ = listener.accept()
                with connection:
                    try:
                        if not hasattr(socket, "SO_PEERCRED"):
                            raise PermissionError("peer credential checks are unavailable")
                        credentials = connection.getsockopt(
                            socket.SOL_SOCKET, socket.SO_PEERCRED, 12,
                        )
                        _, peer_uid, _ = struct.unpack("3i", credentials)
                        if peer_uid != self.config.coordinator_uid:
                            raise PermissionError("untrusted dispatch broker peer")
                        request = _receive(connection, MAX_REQUEST_BYTES)
                        result = self.handle(request)
                        _send(connection, {"ok": True, "result": result},
                              MAX_RESPONSE_BYTES)
                    except Exception:
                        # Never reflect request content, GitHub bodies, or secrets.
                        try:
                            _send(connection, {"ok": False}, MAX_RESPONSE_BYTES)
                        except Exception:
                            pass
        finally:
            listener.close()
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the GitHub dispatch broker")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args(argv)
    config = ServiceConfig.load(args.config)
    DispatchService(config).serve(args.socket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
