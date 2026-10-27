from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import re
import secrets
import selectors
import signal
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omcp.controller import (
    DEEPSEEK_MODEL, DEEPSEEK_PROVIDER, OPENCODE_DIAGNOSTIC_CATEGORIES,
    OPENCODE_PROVIDER_ENDPOINTS,
    canonical_codex_executable, canonical_opencode_executable,
)
from omcp import worker_contract
from omcp.routine_policy import RoutineConfinementPolicy
from omcp.platform import SandboxPolicy, host_platform, sandbox_command


class RunnerTerminated(BaseException):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


class ProviderRequestError(Exception):
    """Detail-free provider failure: no remote response is retained."""


class WorkerSandboxStartupError(RuntimeError):
    """The worker's external sandbox could not start; no task ran."""


class ContractProbeError(RuntimeError):
    """The shared policy could not write the completion contract path."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class OpenCodeDiagnosticCollector:
    """Reduce OpenCode output to a bounded, non-sensitive failure classification."""

    _PRIORITY = {
        "unknown": 0, "provider_request": 1, "standalone_startup": 2, "network": 3,
        "request_rejected": 4, "provider_server": 4, "timeout": 5, "tls": 5,
        "model_unavailable": 6, "endpoint_or_model_not_found": 6, "rate_limit": 6,
        "authentication": 6, "sandbox_denied": 7, "sandbox_profile": 8,
        "local_server_bind": 8, "config": 8,
    }

    def __init__(self) -> None:
        self.category = "unknown"
        self.status: int | None = None
        self._line = bytearray()
        self._discard_line = False

    def _set(self, category: str, status: int | None = None) -> None:
        if category not in OPENCODE_DIAGNOSTIC_CATEGORIES:
            category = "unknown"
        if self._PRIORITY[category] >= self._PRIORITY[self.category]:
            self.category = category
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            self.status = status

    @staticmethod
    def _status_category(status: int) -> str:
        if status in {401, 403}:
            return "authentication"
        if status == 404:
            return "endpoint_or_model_not_found"
        if status == 408:
            return "timeout"
        if status == 429:
            return "rate_limit"
        if 400 <= status <= 499:
            return "request_rejected"
        if 500 <= status <= 599:
            return "provider_server"
        return "provider_request"

    def _classify(self, raw: bytes) -> None:
        # Parse only the small envelope fields. Messages, response bodies, headers,
        # URLs, prompts and model text are neither copied into diagnostics nor stored.
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict) and value.get("type") == "error":
            error = value.get("error")
            if isinstance(error, dict):
                status = error.get("status")
                status = status if isinstance(status, int) and not isinstance(status, bool) else None
                error_type = error.get("type")
                if status is not None and 100 <= status <= 599:
                    self._set(self._status_category(status), status)
                    return
                if isinstance(error_type, str):
                    normalized = error_type.lower()
                    if "auth" in normalized:
                        self._set("authentication", status)
                    elif "model" in normalized and (
                            "unavailable" in normalized or "notfound" in normalized):
                        self._set("model_unavailable", status)
                    elif "timeout" in normalized:
                        self._set("timeout", status)
                    elif "provider" in normalized:
                        self._set("provider_request", status)
                return

        lowered = raw.lower()
        if any(marker in lowered for marker in (
                b"provider.auth", b"provider.request", b'"type":"error"')):
            match = re.search(rb'\bstatus(?:code)?["\s:=]+(\d{3})\b', lowered)
            if match is not None:
                status = int(match.group(1))
                if 100 <= status <= 599:
                    self._set(self._status_category(status), status)
                    return
        if b"host must be * or localhost in network address" in lowered:
            self._set("sandbox_profile")
        elif b"sandbox-exec:" in lowered and (b"invalid" in lowered or b"backtrace:" in lowered):
            self._set("sandbox_profile")
        elif b"operation not permitted" in lowered or b"sandbox denial" in lowered:
            self._set("sandbox_denied")
        elif b"failed to start server" in lowered or b"port 0 in use" in lowered:
            self._set("local_server_bind")
        elif b"standalone server exited before reporting readiness" in lowered:
            self._set("standalone_startup")
        elif b"certificate" in lowered or b"tls" in lowered or b"ssl" in lowered:
            self._set("tls")
        elif b"model unavailable" in lowered:
            self._set("model_unavailable")
        elif b"timed out" in lowered or b"timeout" in lowered:
            self._set("timeout")
        elif any(marker in lowered for marker in (
                b"econnrefused", b"enotfound", b"network unreachable")):
            self._set("network")
        elif b"configuration error" in lowered or b"config error" in lowered:
            self._set("config")

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if byte == 0x0A:
                if not self._discard_line and self._line:
                    self._classify(bytes(self._line))
                self._line.clear()
                self._discard_line = False
            elif not self._discard_line:
                if len(self._line) < 65_536:
                    self._line.append(byte)
                else:
                    self._line.clear()
                    self._discard_line = True

    def finish(self) -> None:
        if not self._discard_line and self._line:
            self._classify(bytes(self._line))
        self._line.clear()
        self._discard_line = False

    def result(self, exit_code: int, timed_out: bool = False) -> dict[str, Any]:
        if timed_out:
            self._set("timeout")
        cause = "timeout" if timed_out else ("signal" if exit_code < 0 else "process_exit")
        result: dict[str, Any] = {"category": self.category, "exit_cause": cause}
        if self.status is not None:
            result["status"] = self.status
        return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(fd)
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_startup_lease(spec_path: Path, spec: dict[str, Any]) -> None:
    """Publish enough identity for a restarted coordinator to adopt this runner."""
    task_id = spec.get("task_id")
    lease_id = spec.get("lease_id")
    lease_path_raw = spec.get("lease_path")
    if task_id is None and lease_id is None and lease_path_raw is None:
        return
    if (not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", task_id)
            or not isinstance(lease_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", lease_id)
            or not isinstance(lease_path_raw, str)):
        raise ValueError("runner spec contains an invalid startup lease")
    lease_path = Path(lease_path_raw)
    if lease_path.parent.resolve() != spec_path.parent.resolve() or lease_path.name != "lease.json":
        raise ValueError("startup lease must stay in the private task directory")
    atomic_json(lease_path, {
        "task_id": task_id, "lease_id": lease_id,
        "pid": os.getpid(), "started_at": utc_now(),
    })


def write_private_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(fd)
    temporary.replace(path)
    os.chmod(path, 0o600)


SANDBOX_STARTUP_PREFIXES = (
    b"bwrap: Can't open source ",
    b"bwrap: Can't mkdir parents ",
    b"bwrap: No permissions to create new namespace",
    b"bwrap: Creating new namespace failed",
    b"bwrap: Failed to unshare",
)
MAX_WORKER_LOG_SCAN_BYTES = 4 * 1024 * 1024
MAX_CREDIT_SCAN_BYTES = 256 * 1024
TASK_TEMP_DIR = ".orchestra-tmp"
CREDIT_EXHAUSTED_CODES = {
    "workspace_owner_credits_depleted",
    "workspace_member_credits_depleted",
}
CREDIT_EXHAUSTED_TEXT = (
    "your workspace is out of credits",
    "you hit your spend cap",
)
CREDIT_ERROR_EVENT_TYPES = {
    "error", "turn.failed", "turn_failed", "stream_error", "error.event",
}
CREDIT_EXHAUSTED_EXACT_LINES = (
    "ERROR: Your workspace is out of credits. Add credits to continue.",
    "ERROR: Your workspace is out of credits. Ask your workspace owner to refill in order to continue.",
)


def _read_tail(path: Path, limit: int = MAX_CREDIT_SCAN_BYTES) -> bytes:
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - limit))
            return source.read(limit)
    except OSError:
        return b""


def _credit_text_matches(value: object) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(marker in lowered for marker in CREDIT_EXHAUSTED_TEXT)


def _credit_object_matches(value: object) -> bool:
    if not isinstance(value, dict):
        return _credit_text_matches(value)
    for key in ("code", "type", "error_type"):
        code = value.get(key)
        if isinstance(code, str) and code.lower() in CREDIT_EXHAUSTED_CODES:
            return True
    for key in ("message", "detail", "reason", "type"):
        if _credit_text_matches(value.get(key)):
            return True
    return False


def codex_credit_exhaustion(
    log_path: Path, stderr_path: Path | None, exit_code: int,
) -> dict[str, Any] | None:
    """Classify only a nonzero Codex exit with a trusted credit error.

    Structured JSONL errors are preferred. A plain-text fallback is read only
    from the separate stderr stream, never from the stdout log that contains the
    task prompt, so a prompt cannot spoof this category.
    """
    if exit_code in {0, 124, 130, 143}:
        return None
    for line in _read_tail(log_path).splitlines():
        stripped = line.strip()
        if not stripped.startswith(b"{"):
            continue
        try:
            value = json.loads(stripped)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        event_type = value.get("type")
        if event_type in CREDIT_ERROR_EVENT_TYPES or "error" in value:
            if _credit_object_matches(value) or _credit_object_matches(value.get("error")):
                return {
                    "category": "credit_exhausted",
                    "stage": "provider",
                    "provider": "openai",
                }
    if stderr_path is not None:
        for line in _read_tail(stderr_path).splitlines():
            if line.strip().decode("utf-8", "replace") in CREDIT_EXHAUSTED_EXACT_LINES:
                return {
                    "category": "credit_exhausted",
                    "stage": "provider",
                    "provider": "openai",
                }
    return None


def probe_codex_sandbox() -> None:
    """Fail closed unless Bubblewrap can start in the worker environment.

    Codex reports a failed tool execution to the model and can still exit zero
    after producing a final message.  A startup probe on the same host, service
    and seccomp context makes that failure a runner failure instead of a
    misleading successful task.
    """
    if host_platform() != "linux":
        return
    bwrap = shutil.which("bwrap")
    if not bwrap:
        # Codex may use a different sandbox implementation when bwrap is absent.
        return
    true = shutil.which("true")
    if not true:
        raise WorkerSandboxStartupError("sandbox probe helper is unavailable")
    command = [
        bwrap, "--unshare-all", "--die-with-parent",
        "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
        "--", true,
    ]
    try:
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise WorkerSandboxStartupError("sandbox startup probe failed") from None
    if completed.returncode != 0:
        raise WorkerSandboxStartupError("sandbox startup probe failed")


def worker_log_has_sandbox_startup_failure(path: Path) -> bool:
    """Detect a Bubblewrap startup failure in the bounded worker log head."""
    try:
        with path.open("rb") as log:
            data = log.read(MAX_WORKER_LOG_SCAN_BYTES)
    except OSError:
        return False
    for line in data.splitlines():
        line = line.lstrip()
        if any(line.startswith(prefix) for prefix in SANDBOX_STARTUP_PREFIXES):
            return True
    return False


def required_result_present(path: Path) -> bool:
    """True only for the non-empty result file every local worker must emit."""
    try:
        return path.is_file() and not path.is_symlink() and path.stat().st_size > 0
    except OSError:
        return False


def write_failure_result(path: Path, message: str) -> None:
    """Best-effort safe result text when the worker produced no output."""
    try:
        write_private_text(path, message)
    except OSError:
        pass


def worker_environment(codex_home: str | None) -> dict[str, str]:
    permitted = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME")
    environment = {key: os.environ[key] for key in permitted if key in os.environ}
    if codex_home:
        environment["CODEX_HOME"] = codex_home
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def opencode_environment(runtime_dir: Path) -> dict[str, str]:
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
    directories = {name: runtime_dir / name for name in ("home", "tmp", "config", "data", "state")}
    for directory in directories.values():
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    environment.update({
        "HOME": str(directories["home"]), "TMPDIR": str(directories["tmp"]),
        "XDG_CONFIG_HOME": str(directories["config"]), "XDG_DATA_HOME": str(directories["data"]),
        "XDG_STATE_HOME": str(directories["state"]), "GIT_TERMINAL_PROMPT": "0",
    })
    return environment


def routine_policy(
    writable_paths: list[str], writable_files: list[str] | None = None,
    git_read_paths: list[str] | None = None,
    read_only_paths: list[str] | None = None,
) -> RoutineConfinementPolicy:
    """Build the one authoritative routine confinement policy."""
    return RoutineConfinementPolicy(
        tuple(writable_paths),
        tuple(read_only_paths if read_only_paths is not None else (git_read_paths or ())),
        writable_files=tuple(writable_files or ()),
    )


def routine_command(executable: str, workspace: Path, result_path: Path,
                    writable_paths: list[str], writable_files: list[str] | None = None,
                    git_read_paths: list[str] | None = None,
                    read_only_paths: list[str] | None = None) -> list[str]:
    """Build the Codex worker command from the shared routine policy."""
    policy = routine_policy(
        writable_paths, writable_files, git_read_paths, read_only_paths,
    )
    return policy.worker_command(executable, workspace, result_path)


def owner_only_credential(path: Path) -> None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        raise ValueError("credential_file is unavailable") from None
    if not path.is_file() or stat.st_mode & 0o077 or stat.st_uid != os.getuid():
        raise ValueError("credential_file must be an owner-only regular file")


def opencode_policy(provider: str, model: str, gateway_url: str) -> dict[str, Any]:
    gateway_match = re.fullmatch(
        r"http://127\.0\.0\.1:(\d{1,5})/[A-Za-z0-9_-]{32,}/v1", gateway_url,
    )
    if (provider not in OPENCODE_PROVIDER_ENDPOINTS
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+", model)
            or not model.startswith(provider + "/") or gateway_match is None
            or not 1 <= int(gateway_match.group(1)) <= 65_535):
        raise ValueError("invalid OpenCode gateway URL")
    model_id = model.rsplit("/", 1)[1]
    return {
        "$schema": "https://opencode.ai/config.json", "model": model, "update": "disable",
        "provider": {provider: {"npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": gateway_url, "apiKey": "omcp-local-gateway",
                        "timeout": 1_800_000}, "models": {model_id: {}}}},
        "policies": [
            {"action": "provider.use", "resource": "*", "effect": "deny"},
            {"action": "provider.use", "resource": provider, "effect": "allow"},
        ],
        "permissions": [{"action": action, "resource": "*", "effect": "deny"}
                        for action in ("external_directory", "shell", "webfetch", "websearch", "skill", "subagent")]
                       + [{"action": action, "resource": resource, "effect": "deny"}
                          for action in ("read", "edit") for resource in (".git/*",)]
                       + [{"action": "read", "resource": resource, "effect": "deny"}
                          for resource in ("*.env", "*.env.*")],
        "snapshots": False,
    }


def opencode_commands(executable: str, workspace: Path, prompt_path: Path, runtime_dir: Path,
                      provider: str, model: str, endpoint_host: str, gateway_url: str,
                      server_port: int) -> tuple[list[str], list[str]]:
    if (provider not in OPENCODE_PROVIDER_ENDPOINTS or OPENCODE_PROVIDER_ENDPOINTS[provider] != endpoint_host
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+", model)
            or not model.startswith(provider + "/")
            or not 1 <= server_port <= 65_535):
        raise ValueError("invalid OpenCode provider or model")
    gateway_match = re.fullmatch(
        r"http://127\.0\.0\.1:(\d{1,5})/[A-Za-z0-9_-]{32,}/v1", gateway_url,
    )
    if gateway_match is None or not 1 <= int(gateway_match.group(1)) <= 65_535:
        raise ValueError("invalid OpenCode gateway URL")
    gateway_port = int(gateway_match.group(1))
    config_dir = runtime_dir / "config" / "opencode"
    config_dir.mkdir(parents=True, mode=0o700)
    atomic_json(config_dir / "opencode.json", opencode_policy(provider, model, gateway_url))
    common = {
        "read_paths": (workspace, runtime_dir, Path(executable).resolve().parent),
        "read_files": (prompt_path,),
        "write_paths": (workspace, runtime_dir),
        "backend": "seatbelt",
        "workdir": workspace,
    }
    server = sandbox_command(SandboxPolicy(
        **common,
        loopback_bind_ports=(server_port,),
        loopback_connect_ports=(gateway_port,),
    ), [
        executable, "serve", "--hostname", "127.0.0.1", "--port", str(server_port),
    ])
    client = sandbox_command(SandboxPolicy(
        **common,
        loopback_connect_ports=(server_port,),
    ), [
        executable, "run",
        "--server", f"http://127.0.0.1:{server_port}", "--format", "json",
        "--model", model, "--file", str(prompt_path),
        "Read the attached task instructions and work only in the current workspace.",
    ])
    return server, client


def reserve_loopback_port() -> int:
    """Select a candidate loopback port; the password-protected server binds it next."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class PinnedProviderGateway:
    """One-task reverse proxy from loopback to one verified provider endpoint."""

    MAX_REQUEST_BYTES = 4 * 1024 * 1024
    MAX_RESPONSE_BYTES = 16 * 1024 * 1024
    MAX_REQUESTS = 64
    API_PATH = "/v1/chat/completions"
    GATEWAY_SUFFIX = "/v1"

    def __init__(self, provider: str, model: str, endpoint_host: str,
                 credential_file: Path, seconds: int) -> None:
        if (provider not in OPENCODE_PROVIDER_ENDPOINTS
                or OPENCODE_PROVIDER_ENDPOINTS[provider] != endpoint_host
                or not (model == DEEPSEEK_MODEL
                        or (re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+", model)
                            and model.startswith(provider + "/")))
                or not 60 <= seconds <= 7200):
            raise ValueError("invalid pinned provider gateway")
        owner_only_credential(credential_file)
        api_key = credential_file.read_text(encoding="utf-8").strip()
        if not api_key or len(api_key) > 8192:
            raise ValueError("credential_file is invalid")
        self.provider = provider
        self.model_id = model.rsplit("/", 1)[-1]
        self.endpoint_host = endpoint_host
        self._api_key = api_key
        self._timeout = min(seconds, 300)
        self._token = secrets.token_urlsafe(32)
        self._request_count = 0
        self._request_lock = threading.Lock()
        self.failure_status: int | None = None
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url: str | None = None

    def _claim_request(self) -> bool:
        with self._request_lock:
            if self._request_count >= self.MAX_REQUESTS:
                return False
            self._request_count += 1
            return True

    def _serve_request(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        # One request per connection avoids ambiguous framing and request smuggling;
        # OpenCode may open a fresh loopback connection for its next model turn.
        handler.close_connection = True
        expected_host = f"127.0.0.1:{self._server.server_port}" if self._server else ""
        expected_path = f"/{self._token}{self.API_PATH}"

        def header_values(name: str) -> list[str]:
            get_all = getattr(handler.headers, "get_all", None)
            if callable(get_all):
                return list(get_all(name, []))
            value = handler.headers.get(name)
            return [] if value is None else [value]

        host = header_values("Host")
        authorization = header_values("Authorization")
        content_type = header_values("Content-Type")
        length_values = header_values("Content-Length")
        if (handler.path != expected_path or host != [expected_host]
                or authorization != ["Bearer omcp-local-gateway"]
                or len(content_type) != 1
                or not content_type[0].lower().startswith("application/json")
                or len(length_values) != 1
                or any(header_values(name) for name in (
                    "Transfer-Encoding", "Content-Encoding", "Expect", "Trailer", "Upgrade",
                ))
                or not self._claim_request()):
            self._reject(handler, 404)
            return
        length_raw = length_values[0]
        if not isinstance(length_raw, str) or not length_raw.isascii() or not length_raw.isdigit():
            self._reject(handler, 400)
            return
        length = int(length_raw)
        if not 1 <= length <= self.MAX_REQUEST_BYTES:
            self._reject(handler, 413)
            return
        body = handler.rfile.read(length)
        if len(body) != length:
            self._reject(handler, 400)
            return
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reject(handler, 400)
            return
        if not self._valid_payload(payload):
            self._reject(handler, 400)
            return
        upstream_body = json.dumps(payload, separators=(",", ":")).encode()
        connection: http.client.HTTPSConnection | None = None
        response_started = False
        try:
            connection = http.client.HTTPSConnection(
                self.endpoint_host, 443, timeout=self._timeout,
                context=ssl.create_default_context(),
            )
            connection.request("POST", self.API_PATH, upstream_body, {
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json",
            })
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                self.failure_status = response.status
            content_type = response.getheader("Content-Type", "application/json")
            if not (content_type.lower().startswith("application/json")
                    or content_type.lower().startswith("text/event-stream")):
                raise ProviderRequestError()
            if response.getheader("Content-Encoding") is not None:
                raise ProviderRequestError()
            handler.send_response_only(response.status)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Connection", "close")
            handler.end_headers()
            response_started = True
            remaining = self.MAX_RESPONSE_BYTES
            while remaining:
                chunk = response.read(min(65_536, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
            if remaining == 0 and response.read(1):
                handler.close_connection = True
        except (OSError, ssl.SSLError, ValueError, http.client.HTTPException,
                ProviderRequestError):
            if not response_started:
                self._reject(handler, 502)
        finally:
            if connection is not None:
                connection.close()

    def _valid_payload(self, payload: object) -> bool:
        return bool(
            isinstance(payload, dict)
            and payload.get("model") == self.model_id
            and isinstance(payload.get("messages"), list)
        )

    def provider_failure_category(self) -> str | None:
        status = self.failure_status
        if status in {401, 403}:
            return "authentication"
        if status == 402:
            return "credit_exhausted"
        if status == 429:
            return "rate_limit"
        if status is not None and 500 <= status <= 599:
            return "transient_provider"
        return None

    @staticmethod
    def _reject(handler: http.server.BaseHTTPRequestHandler, status: int) -> None:
        body = b'{"error":{"message":"request rejected"}}'
        try:
            handler.send_response_only(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        handler.close_connection = True

    def start(self) -> str:
        if self._server is not None:
            raise RuntimeError("provider gateway already started")
        gateway = self

        class GatewayHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                gateway._serve_request(self)

            def do_CONNECT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                gateway._reject(self, 405)

            def log_message(self, _format: str, *args: object) -> None:
                return

        class GatewayServer(http.server.ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

            def handle_error(self, request: object, client_address: object) -> None:
                return

        self._server = GatewayServer(("127.0.0.1", 0), GatewayHandler)
        port = self._server.server_port
        self.url = f"http://127.0.0.1:{port}/{self._token}{self.GATEWAY_SUFFIX}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="omcp-provider-gateway", daemon=True,
        )
        self._thread.start()
        return self.url

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        self.url = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        self._api_key = ""


class PinnedResponsesGateway(PinnedProviderGateway):
    """Pinned Responses API gateway for the DeepSeek/Codex runtime."""

    API_PATH = "/responses"
    GATEWAY_SUFFIX = ""

    def _valid_payload(self, payload: object) -> bool:
        return bool(
            isinstance(payload, dict)
            and payload.get("model") == self.model_id
            and isinstance(payload.get("input"), (str, list))
        )


def _classify_opencode_logs(runtime_dir: Path, collector: OpenCodeDiagnosticCollector) -> None:
    """Inspect bounded private runtime logs, retaining only categorical diagnostics."""
    resolved_runtime = runtime_dir.resolve()
    inspected = 0
    for path in sorted(runtime_dir.rglob("*.log")):
        if inspected >= 4 or path.is_symlink() or not path.is_file():
            continue
        try:
            if not path.resolve().is_relative_to(resolved_runtime):
                continue
            with path.open("rb") as source:
                remaining = 1_048_576
                while remaining:
                    chunk = source.read(min(8192, remaining))
                    if not chunk:
                        break
                    collector.feed(chunk)
                    remaining -= len(chunk)
            inspected += 1
        except OSError:
            continue


def merge_opencode_runtime_diagnostic(
    runtime_dir: Path, diagnostic: dict[str, Any], exit_code: int,
) -> dict[str, Any]:
    """Upgrade a client-side unknown failure with bounded server-log categories."""
    collector = OpenCodeDiagnosticCollector()
    category = diagnostic.get("category")
    if category in OPENCODE_DIAGNOSTIC_CATEGORIES:
        collector.category = category
    status = diagnostic.get("status")
    if isinstance(status, int) and not isinstance(status, bool):
        collector.status = status
    _classify_opencode_logs(runtime_dir, collector)
    collector.finish()
    return collector.result(exit_code, timed_out=diagnostic.get("exit_cause") == "timeout")


def run_opencode_process(command: list[str], workspace: Path, environment: dict[str, str],
                         runtime_dir: Path, seconds: int) -> tuple[int, dict[str, Any]]:
    """Run OpenCode while reducing all output to a small redacted diagnostic."""
    collector = OpenCodeDiagnosticCollector()
    timed_out = False
    process = subprocess.Popen(
        command, cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True, start_new_session=True,
    )
    if process.stdout is None:  # pragma: no cover - PIPE always supplies stdout
        raise RuntimeError("OpenCode diagnostic pipe is unavailable")
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    deadline = time.monotonic() + seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and process.poll() is None:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
            events = (selector.select(max(0, min(0.25, remaining)))
                      if process.poll() is None else selector.select(0))
            for key, _mask in events:
                try:
                    chunk = os.read(key.fd, 8192)
                except BlockingIOError:
                    continue
                if chunk:
                    collector.feed(chunk)
                else:
                    selector.unregister(key.fd)
            if process.poll() is not None and not events:
                break
        exit_code = process.wait()
    except BaseException:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
    collector.finish()
    _classify_opencode_logs(runtime_dir, collector)
    collector.finish()
    return (124 if timed_out else exit_code), collector.result(exit_code, timed_out)


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_opencode_with_server(server_command: list[str], client_command: list[str],
                             workspace: Path, environment: dict[str, str], runtime_dir: Path,
                             server_port: int, seconds: int) -> tuple[int, dict[str, Any]]:
    """Run a fixed-port private V2 server and an exactly scoped client."""
    started = time.monotonic()
    deadline = started + seconds
    server = subprocess.Popen(
        server_command, cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, close_fds=True,
        start_new_session=True,
    )
    try:
        ready = False
        startup_deadline = min(deadline, started + 10)
        while time.monotonic() < startup_deadline:
            return_code = server.poll()
            if return_code is not None:
                return return_code or 1, {
                    "category": "standalone_startup",
                    "exit_cause": "signal" if return_code < 0 else "process_exit",
                }
            try:
                with socket.create_connection(("127.0.0.1", server_port), timeout=0.1):
                    ready = True
                # If another local process won the selected port, OpenCode's
                # server exits after its failed bind. Never send task data until
                # the launched process remains alive beyond that failure window.
                time.sleep(0.1)
                if server.poll() is None:
                    break
                return_code = server.returncode
                return return_code or 1, {
                    "category": "local_server_bind",
                    "exit_cause": "signal" if return_code < 0 else "process_exit",
                }
            except OSError:
                time.sleep(0.05)
        if not ready:
            return 1, {"category": "standalone_startup", "exit_cause": "timeout"}
        remaining = max(1, int(deadline - time.monotonic()))
        return run_opencode_process(
            client_command, workspace, environment, runtime_dir, remaining,
        )
    finally:
        _terminate_process_group(server)


def sanitize_provider_text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProviderRequestError()
    value = "".join(char if char in "\n\t" or ord(char) >= 0x20 else " " for char in value)
    return value.encode("utf-8")[:maximum].decode("utf-8", "ignore")


def codex_provider_args(base_url: str, model: str) -> list[str]:
    """Pin a Codex worker to the DeepSeek Responses API through our gateway."""
    if not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}/[A-Za-z0-9_-]{32,}/?", base_url):
        raise ValueError("invalid Codex provider gateway URL")
    if model != DEEPSEEK_MODEL:
        raise ValueError("unsupported Codex provider model")
    values = {
        "model_provider": '"deepseek"',
        "model": f'"{model}"',
        "model_providers.deepseek.name": '"DeepSeek"',
        "model_providers.deepseek.base_url": f'"{base_url}"',
        "model_providers.deepseek.env_key": '"DEEPSEEK_API_KEY"',
        "model_providers.deepseek.wire_api": '"responses"',
        "model_providers.deepseek.requires_openai_auth": "false",
        "shell_environment_policy.inherit": '"none"',
        "agents.enabled": "false",
        "features.multi_agent": "false",
        "features.browser_use": "false",
        "features.computer_use": "false",
        "features.apps": "false",
        "features.plugins": "false",
        "features.skill_search": "false",
        "features.skill_mcp_dependency_install": "false",
        "features.image_generation": "false",
        "features.in_app_browser": "false",
        "features.in_app_chat": "false",
        "features.goals": "false",
        "features.view_image": "false",
        "features.exec_permission_approvals": "false",
        "features.default_mode_request_user_input": "false",
        "tools.experimental_request_user_input.enabled": "false",
        "web_search": '"disabled"',
    }
    args: list[str] = []
    for key, value in values.items():
        args.extend(["-c", f"{key}={value}"])
    return args


def run_deepseek(spec: dict[str, Any], prompt_path: Path, result_path: Path,
                 seconds: int, maximum: int) -> int:
    connection: http.client.HTTPSConnection | None = None
    previous_alarm = signal.getsignal(signal.SIGALRM)

    def deadline(_signum: int, _frame: Any) -> None:
        raise ProviderRequestError()

    # Socket timeouts cap an idle operation, not a peer that trickles bytes.  This
    # process-wide timer is the end-to-end bound for the direct adapter.
    signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        if (spec.get("provider") != DEEPSEEK_PROVIDER or spec.get("model") != DEEPSEEK_MODEL
                or spec.get("endpoint_host") != "api.deepseek.com" or spec.get("command") not in ([], None)):
            raise ProviderRequestError()
        credential_raw = spec.get("credential_file")
        if not isinstance(credential_raw, str):
            raise ProviderRequestError()
        credential = Path(credential_raw)
        owner_only_credential(credential)
        api_key = credential.read_text(encoding="utf-8").strip()
        if not api_key or len(api_key) > 8192:
            raise ProviderRequestError()
        payload = json.dumps({"model": DEEPSEEK_MODEL, "stream": False,
            "max_tokens": max(64, min(4096, maximum // 4)), "messages": [
                {"role": "system", "content": "Return a concise, safe response. You have no tools and must not claim to have changed files."},
                {"role": "user", "content": prompt_path.read_text(encoding="utf-8")}]}).encode()
        connection = http.client.HTTPSConnection("api.deepseek.com", 443, timeout=seconds,
                                                  context=ssl.create_default_context())
        connection.request("POST", "/v1/chat/completions", payload, {
            "Authorization": "Bearer " + api_key, "Content-Type": "application/json", "Accept": "application/json"})
        response = connection.getresponse()
        body = response.read(maximum + 65_537)
        if response.status != 200 or len(body) > maximum + 65_536:
            raise ProviderRequestError()
        content = json.loads(body)["choices"][0]["message"]["content"]
        write_private_text(result_path, sanitize_provider_text(content, maximum))
        return 0
    except (OSError, ssl.SSLError, ValueError, KeyError, IndexError, TypeError,
            json.JSONDecodeError, http.client.HTTPException, ProviderRequestError):
        write_private_text(result_path, "DeepSeek request failed; no provider response was retained.\n")
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm)
        if connection is not None:
            connection.close()


def run(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text())
    write_startup_lease(spec_path, spec)
    prompt, log, completion, result, workspace = (Path(spec[key]) for key in
        ("prompt_path", "log_path", "completion_path", "result_path", "workspace"))
    adapter = spec.get("adapter", "codex")
    seconds, maximum = spec.get("max_worker_seconds", 1800), spec.get("max_response_bytes", 65536)
    if adapter not in {"codex", "opencode", "deepseek"}:
        raise ValueError("unsupported worker adapter")
    if (not isinstance(seconds, int) or isinstance(seconds, bool) or not 60 <= seconds <= 7200
            or not isinstance(maximum, int) or isinstance(maximum, bool) or not 1024 <= maximum <= 1048576):
        raise ValueError("invalid worker bounds")
    runtime_dir = routine_tmp = contract_tmp = task_temp_tmp = None
    contract_probe_command: list[str] | None = None
    codex_events = log.parent / "worker-events.jsonl" if adapter == "codex" else None
    gateway: PinnedProviderGateway | None = None
    worker_diagnostic: dict[str, Any] | None = None
    completion_contract: str | None = None
    completion_contract_error: str | None = None
    require_contract = adapter in {"codex", "opencode"} and spec.get("require_completion_contract") is True
    exit_code, error_type = 125, None
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    def terminate(signum: int, _frame: Any) -> None: raise RunnerTerminated(signum)
    signal.signal(signal.SIGTERM, terminate)
    try:
        if adapter == "deepseek":
            exit_code = run_deepseek(spec, prompt, result, seconds, maximum)
            error_type = "DeepSeekRequestFailed" if exit_code else None
        else:
            configured = spec.get("command")
            if not isinstance(configured, list) or len(configured) != 1:
                raise ValueError("runner spec must contain exactly one executable")
            executable = canonical_codex_executable(configured[0]) if adapter == "codex" else canonical_opencode_executable(configured[0])
            capabilities = spec.get("execution_capabilities")
            if adapter == "opencode":
                if capabilities is not None:
                    raise ValueError("OpenCode workers cannot use standing authorization")
                values = (spec.get("credential_file"), spec.get("provider"), spec.get("model"), spec.get("endpoint_host"))
                if not all(isinstance(value, str) and value for value in values):
                    raise ValueError("OpenCode worker specification is incomplete")
                runtime_dir = log.parent / "opencode-runtime"; runtime_dir.mkdir(mode=0o700)
                gateway = PinnedProviderGateway(
                    values[1], values[2], values[3], Path(values[0]), seconds,
                )
                gateway_url = gateway.start()
                server_port = reserve_loopback_port()
                server_command, command = opencode_commands(
                    executable, workspace, prompt, runtime_dir,
                    values[1], values[2], values[3], gateway_url, server_port,
                )
                environment = opencode_environment(runtime_dir)
                environment["OPENCODE_PASSWORD"] = secrets.token_urlsafe(32)
            elif capabilities is None:
                command = [executable, "--ask-for-approval", "never", "exec", "--json",
                           "--sandbox", "workspace-write",
                           "--cd", str(workspace), "--output-last-message", str(result), "-"]
                environment = worker_environment(spec.get("codex_home"))
                contract_tmp = workspace / worker_contract.CONTRACT_DIR
                contract_tmp.mkdir(mode=0o700, exist_ok=True)
                routine_tmp = contract_tmp
                task_temp_tmp = workspace / TASK_TEMP_DIR
                task_temp_tmp.mkdir(mode=0o700, exist_ok=True)
                environment["TMPDIR"] = str(task_temp_tmp)
                if spec.get("provider") == DEEPSEEK_PROVIDER:
                    credential = Path(str(spec.get("credential_file", "")))
                    gateway = PinnedResponsesGateway(
                        DEEPSEEK_PROVIDER, spec.get("model"), "api.deepseek.com",
                        credential, seconds,
                    )
                    gateway_url = gateway.start()
                    insert_at = command.index("exec") + 1
                    command = [*command[:insert_at], *codex_provider_args(gateway_url, spec.get("model")), *command[insert_at:]]
                    environment["DEEPSEEK_API_KEY"] = "omcp-local-gateway"
            else:
                if (not isinstance(capabilities, dict) or capabilities.get("profile") != "routine_development_v1"
                        or capabilities.get("network") is not False or capabilities.get("external_credentials") is not False
                        or capabilities.get("git_metadata") != "read_only"
                        or not isinstance(capabilities.get("writable_paths"), list)
                        or not isinstance(capabilities.get("writable_files", []), list)):
                    raise ValueError("invalid routine execution capabilities")
                contract_tmp = workspace / worker_contract.CONTRACT_DIR
                contract_tmp.mkdir(mode=0o700, exist_ok=True)
                routine_tmp = contract_tmp
                task_temp_tmp = workspace / TASK_TEMP_DIR
                task_temp_tmp.mkdir(mode=0o700, exist_ok=True)
                read_only_paths = spec.get(
                    "read_only_paths", spec.get("git_read_paths", []),
                )
                if not isinstance(read_only_paths, list):
                    raise ValueError("invalid routine read paths")
                policy = routine_policy(
                    capabilities["writable_paths"],
                    capabilities.get("writable_files", []),
                    read_only_paths=read_only_paths,
                )
                command = policy.worker_command(executable, workspace, result)
                contract_probe_command = policy.probe_command(
                    executable, workspace, [
                        sys.executable, "-c",
                        "import os,sys; p=os.path.join(sys.argv[1], 'contract-write-probe'); "
                        "open(p, 'x').write('ok'); os.unlink(p)",
                        str(contract_tmp),
                    ],
                )
                environment = worker_environment(spec.get("codex_home")); environment["TMPDIR"] = str(task_temp_tmp)
                if spec.get("provider") == DEEPSEEK_PROVIDER:
                    credential = Path(str(spec.get("credential_file", "")))
                    gateway = PinnedResponsesGateway(
                        DEEPSEEK_PROVIDER, spec.get("model"), "api.deepseek.com",
                        credential, seconds,
                    )
                    gateway_url = gateway.start()
                    insert_at = command.index("exec") + 1
                    command = [*command[:insert_at], *codex_provider_args(gateway_url, spec.get("model")), *command[insert_at:]]
                    environment["DEEPSEEK_API_KEY"] = "omcp-local-gateway"
            if (contract_probe_command is not None
                    and spec.get("contract_probe") is True):
                try:
                    probe = subprocess.run(
                        contract_probe_command, cwd=workspace, env=environment,
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, check=False, timeout=20,
                    )
                except subprocess.TimeoutExpired:
                    raise ContractProbeError("sandbox") from None
                if probe.returncode != 0:
                    detail = ((probe.stderr or b"") + (probe.stdout or b""))
                    if isinstance(detail, str):
                        detail = detail.encode("utf-8", "replace")
                    lowered = detail.lower()
                    if b"permission denied" in lowered or b"read-only file system" in lowered:
                        raise ContractProbeError("contract_path_denied")
                    raise ContractProbeError("sandbox")
            if adapter == "opencode":
                exit_code, worker_diagnostic = run_opencode_with_server(
                    server_command, command, workspace, environment, runtime_dir,
                    server_port, seconds,
                )
                worker_diagnostic = merge_opencode_runtime_diagnostic(
                    runtime_dir, worker_diagnostic, exit_code,
                )
                error_type = "WorkerTimeout" if exit_code == 124 else (
                    "OpenCodeExecutionFailed" if exit_code else None
                )
            elif adapter == "codex" and spec.get("sandbox_startup_probe") is True:
                try:
                    probe_codex_sandbox()
                except WorkerSandboxStartupError:
                    exit_code, error_type = 125, "SandboxStartupError"
                    if not required_result_present(result):
                        write_failure_result(
                            result,
                            "Worker sandbox failed to start; the task did not run.\n",
                        )
                else:
                    assert codex_events is not None
                    with prompt.open("rb") as input_file, \
                            codex_events.open("ab") as output, log.open("ab") as error_output:
                        exit_code = subprocess.run(
                            command, cwd=workspace, env=environment, stdin=input_file,
                            stdout=output, stderr=error_output, check=False,
                            timeout=seconds,
                        ).returncode
            elif adapter == "codex":
                assert codex_events is not None
                with prompt.open("rb") as input_file, \
                        codex_events.open("ab") as output, log.open("ab") as error_output:
                    exit_code = subprocess.run(
                        command, cwd=workspace, env=environment, stdin=input_file,
                        stdout=output, stderr=error_output, check=False,
                        timeout=seconds,
                    ).returncode
            else:
                with prompt.open("rb") as input_file, log.open("ab") as output:
                    exit_code = subprocess.run(command, cwd=workspace, env=environment, stdin=input_file,
                        stdout=output, stderr=subprocess.STDOUT, check=False, timeout=seconds).returncode
    except ContractProbeError as exc:
        exit_code = 125
        if exc.category == "contract_path_denied":
            error_type = "ContractPathDenied"
            worker_diagnostic = {
                "category": "contract_path_denied", "stage": "completion",
            }
            write_failure_result(
                result,
                "The worker sandbox denied the completion-contract path; "
                "the task did not run.\n",
            )
        else:
            error_type = "ContractProbeFailed"
            worker_diagnostic = {"category": "sandbox", "stage": "startup"}
            write_failure_result(
                result,
                "The completion-contract write probe could not run; "
                "the task did not run.\n",
            )
    except subprocess.TimeoutExpired:
        exit_code, error_type = 124, "WorkerTimeout"
    except RunnerTerminated as exc:
        exit_code, error_type = 128 + exc.signum, type(exc).__name__
    except BaseException as exc:
        error_type = type(exc).__name__; raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        if gateway is not None:
            gateway.close()
        # Read the structured contract and security report while the private
        # contract directory still exists.  These files are never interpreted
        # as natural-language assertions.
        if require_contract:
            try:
                contract = worker_contract.read_completion_contract(
                    worker_contract.contract_path(workspace),
                )
            except (worker_contract.CompletionContractError, OSError, ValueError) as exc:
                completion_contract = "invalid"
                completion_contract_error = type(exc).__name__
            else:
                completion_contract = contract["status"]
        if routine_tmp is not None:
            shutil.rmtree(routine_tmp, ignore_errors=True)
            if routine_tmp.exists():
                exit_code, error_type = 125, "RoutineTemporaryDirectoryCleanupError"
        if task_temp_tmp is not None and task_temp_tmp != routine_tmp:
            shutil.rmtree(task_temp_tmp, ignore_errors=True)
            if task_temp_tmp.exists():
                exit_code, error_type = 125, "RoutineTemporaryDirectoryCleanupError"
        if runtime_dir is not None:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            if runtime_dir.exists():
                exit_code, error_type = 125, "OpenCodeRuntimeDirectoryCleanupError"
            elif not result.exists():
                write_private_text(result, "OpenCode execution completed; provider output is intentionally not retained.\n"
                    if exit_code == 0 else f"OpenCode execution failed (exit {exit_code}); provider output is intentionally not retained.\n")
        sandbox_failure = bool(
            adapter == "codex" and worker_log_has_sandbox_startup_failure(log)
        )
        if sandbox_failure:
            error_type = "SandboxStartupError"
            if exit_code == 0:
                exit_code = 125
            if not required_result_present(result):
                write_failure_result(
                    result, "Worker sandbox failed to start; the task did not run.\n",
                )
        credit_failure = (
            codex_credit_exhaustion(codex_events, log, exit_code)
            if adapter == "codex" and not sandbox_failure else None
        )
        provider_failure = (
            gateway.provider_failure_category()
            if adapter == "codex" and isinstance(gateway, PinnedProviderGateway)
            else None
        )
        if provider_failure == "credit_exhausted":
            error_type = "CreditExhausted"
            worker_diagnostic = {
                "category": "credit_exhausted", "stage": "provider",
                "provider": "deepseek",
            }
            completion_contract = None
            completion_contract_error = None
            if not required_result_present(result):
                write_failure_result(
                    result, "DeepSeek account balance is insufficient; no coding result was produced.\n",
                )
        elif provider_failure in {"authentication", "rate_limit", "transient_provider"}:
            error_type = {
                "authentication": "ProviderAuthenticationFailed",
                "rate_limit": "ProviderRateLimited",
                "transient_provider": "ProviderTransientFailure",
            }[provider_failure]
            worker_diagnostic = {
                "category": provider_failure, "stage": "provider",
                "provider": "deepseek",
            }
        if provider_failure is not None and exit_code == 0:
            exit_code = 1
        if credit_failure is not None:
            error_type = "CreditExhausted"
            worker_diagnostic = credit_failure
            # Credit exhaustion is the root cause. The absent completion
            # contract is a consequence and must not become the primary error.
            completion_contract = None
            completion_contract_error = None
            if not required_result_present(result):
                write_failure_result(
                    result,
                    "Workspace credits are exhausted; no coding result was produced.\n",
                )
        if (not sandbox_failure and credit_failure is None and exit_code == 0
                and require_contract
                and completion_contract != "success"):
            exit_code = 125
            error_type = (
                "TaskContractFailed" if completion_contract == "failure"
                else "TaskContractInvalid"
            )
        # result.txt is mandatory for every local worker.  A process can exit
        # zero after its tool sandbox failed, or without emitting the requested
        # final message; neither is a successful task.
        if exit_code == 0 and not required_result_present(result):
            exit_code, error_type = 125, "MissingWorkerResult"
            write_failure_result(
                result, "Worker exited without the required result output.\n",
            )
        elif (exit_code != 0 and not required_result_present(result)
              and not sandbox_failure and credit_failure is None):
            write_failure_result(
                result, "Worker failed before producing a result; see task diagnostics.\n",
            )
        record: dict[str, Any] = {
            "exit_code": exit_code, "finished_at": utc_now(), "runner_error": error_type,
        }
        if require_contract and credit_failure is None:
            record["completion_contract"] = completion_contract or "missing"
            if completion_contract_error is not None:
                record["completion_contract_error"] = completion_contract_error
        if worker_diagnostic is not None:
            record["worker_diagnostic"] = worker_diagnostic
        atomic_json(completion, record)
        if codex_events is not None:
            # The JSONL stream can contain the prompt and model text. It is
            # only needed for bounded classification, so do not retain it.
            codex_events.unlink(missing_ok=True)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one OMCP worker safely")
    parser.add_argument("--spec", type=Path, required=True)
    return run(parser.parse_args(argv).spec)


if __name__ == "__main__":
    raise SystemExit(main())
