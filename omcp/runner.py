from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import signal
import shutil
import ssl
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omcp.controller import (
    DEEPSEEK_MODEL, DEEPSEEK_PROVIDER, OPENCODE_PROVIDER_BASE_PATHS,
    OPENCODE_PROVIDER_ENDPOINTS,
    canonical_codex_executable, canonical_opencode_executable,
)


OPENCODE_INTERNAL_GIT = "/usr/bin/git"


class RunnerTerminated(BaseException):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


class ProviderRequestError(Exception):
    """Detail-free provider failure: no remote response is retained."""


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


def routine_command(executable: str, workspace: Path, result_path: Path,
                    writable_paths: list[str]) -> list[str]:
    if not all(isinstance(value, str) and value and all(
            part and part.replace("_", "").replace("-", "").isalnum() for part in Path(value).parts)
            for value in writable_paths):
        raise ValueError("invalid routine writable paths")
    rules = ','.join(['"."="read"', '".orchestra-tmp"="write"',
                      *(f'"{value}"="write"' for value in writable_paths)])
    return [
        executable, "--ask-for-approval", "never", "exec", "--ignore-user-config",
        "--ignore-rules", "--ephemeral", "--config", 'default_permissions="orchestra-routine"',
        "--config", 'permissions.orchestra-routine.filesystem.:root="deny"',
        "--config", 'permissions.orchestra-routine.filesystem.:minimal="read"',
        "--config", 'permissions.orchestra-routine.filesystem.:tmpdir="deny"',
        "--config", 'permissions.orchestra-routine.filesystem.:slash_tmp="deny"',
        "--config", "permissions.orchestra-routine.filesystem.:workspace_roots={" + rules + "}",
        "--config", "permissions.orchestra-routine.network.enabled=false", "--cd", str(workspace),
        "--output-last-message", str(result_path), "-",
    ]


def _seatbelt_string(value: Path | str) -> str:
    text = str(value)
    if any(ord(character) < 0x20 or ord(character) == 0x7f for character in text):
        raise ValueError("Seatbelt paths may not contain control characters")
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _seatbelt_metadata_rules(*values: Path | str) -> tuple[str, ...]:
    paths: set[Path] = set()
    for value in values:
        path = Path(value)
        paths.add(path)
        paths.update(path.parents)
    return tuple(
        f'(allow file-read-metadata (literal "{_seatbelt_string(path)}"))'
        for path in sorted(paths, key=lambda item: (len(item.parts), str(item)))
    )


def _seatbelt_parent_lookup_rules(*values: Path | str) -> tuple[str, ...]:
    """Permit exact ancestor lookup without opening any ancestor subtree."""
    parents: set[Path] = set()
    for value in values:
        parents.update(Path(value).parents)
    return tuple(
        f'(allow file-read* (literal "{_seatbelt_string(path)}"))'
        for path in sorted(parents, key=lambda item: (len(item.parts), str(item)))
    )


def owner_only_credential(path: Path) -> None:
    if any(character in str(path) for character in "{}\r\n\x00"):
        raise ValueError("credential_file path contains unsupported characters")
    try:
        stat = path.stat()
    except FileNotFoundError:
        raise ValueError("credential_file is unavailable") from None
    if not path.is_file() or stat.st_mode & 0o077 or stat.st_uid != os.getuid():
        raise ValueError("credential_file must be an owner-only regular file")


def opencode_policy(provider: str, model: str, credential_file: Path) -> dict[str, Any]:
    endpoint = OPENCODE_PROVIDER_ENDPOINTS[provider]
    base_path = OPENCODE_PROVIDER_BASE_PATHS[provider]
    model_id = model.rsplit("/", 1)[1]
    return {
        "$schema": "https://opencode.ai/config.json", "model": model, "update": "disable",
        "provider": {provider: {"npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": f"https://{endpoint}{base_path}",
                         "apiKey": "{file:" + str(credential_file) + "}",
                         "timeout": 1_800_000}, "models": {model_id: {}}}},
        "policies": [{"action": "provider.use", "resource": "*", "effect": "deny"},
                     {"action": "provider.use", "resource": provider, "effect": "allow"},
                     *[{"action": "tool.use", "resource": f"{tool}:*", "effect": "deny"}
                       for tool in ("shell", "webfetch", "websearch", "external_directory")]],
        "permissions": [{"action": action, "resource": "*", "effect": "deny"}
                        for action in ("external_directory", "shell", "webfetch", "websearch", "skill", "subagent")]
                       + [{"action": action, "resource": resource, "effect": "deny"}
                          for action in ("read", "edit") for resource in (".git/*",)]
                       + [{"action": "read", "resource": resource, "effect": "deny"}
                          for resource in ("*.env", "*.env.*")],
        "snapshots": False,
    }


def opencode_command(executable: str, workspace: Path, prompt_path: Path, credential_file: Path,
                     runtime_dir: Path, provider: str, model: str, endpoint_host: str) -> list[str]:
    owner_only_credential(credential_file)
    if (provider not in OPENCODE_PROVIDER_ENDPOINTS or OPENCODE_PROVIDER_ENDPOINTS[provider] != endpoint_host
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+", model)
            or not model.startswith(provider + "/")):
        raise ValueError("invalid OpenCode provider or model")
    config_dir = runtime_dir / "config" / "opencode"
    config_dir.mkdir(parents=True, mode=0o700)
    atomic_json(config_dir / "opencode.json", opencode_policy(provider, model, credential_file))
    profile = "\n".join((
        "(version 1)", "(deny default)", '(import "system.sb")',
        *_seatbelt_metadata_rules(
            executable, workspace, prompt_path, credential_file, runtime_dir,
        ),
        *_seatbelt_parent_lookup_rules(
            executable, workspace, prompt_path, credential_file, runtime_dir,
        ),
        "(allow process-fork)",
        f'(allow process-exec (literal "{_seatbelt_string(executable)}"))',
        f'(allow process-exec (literal "{OPENCODE_INTERNAL_GIT}"))',
        f'(allow file-read* (literal "{_seatbelt_string(executable)}"))',
        f'(allow file-read* (literal "{OPENCODE_INTERNAL_GIT}"))',
        f'(allow file-read* (subpath "{_seatbelt_string(workspace)}"))',
        f'(allow file-read* (literal "{_seatbelt_string(prompt_path)}"))',
        f'(allow file-read* (literal "{_seatbelt_string(credential_file)}"))',
        f'(allow file-read* (subpath "{_seatbelt_string(runtime_dir)}"))',
        f'(allow file-write* (subpath "{_seatbelt_string(workspace)}"))',
        f'(allow file-write* (subpath "{_seatbelt_string(runtime_dir)}"))',
        '(allow network-bind (local tcp "*:*"))',
        '(allow network-inbound (local tcp "*:*"))',
        '(allow network-outbound (remote tcp "localhost:*"))',
        '(allow network-outbound (remote tcp "*:443"))',
    ))
    return ["/usr/bin/sandbox-exec", "-p", profile, executable, "run", "--standalone", "--format", "json",
            "--model", model, "--file", str(prompt_path),
            "Read the attached task instructions and work only in the current workspace."]


def sanitize_provider_text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProviderRequestError()
    value = "".join(char if char in "\n\t" or ord(char) >= 0x20 else " " for char in value)
    return value.encode("utf-8")[:maximum].decode("utf-8", "ignore")


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
    runtime_dir = routine_tmp = None
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
                command = opencode_command(executable, workspace, prompt, Path(values[0]), runtime_dir,
                                           values[1], values[2], values[3])
                environment = opencode_environment(runtime_dir)
            elif capabilities is None:
                command = [executable, "--ask-for-approval", "never", "exec", "--sandbox", "workspace-write",
                           "--cd", str(workspace), "--output-last-message", str(result), "-"]
                environment = worker_environment(spec.get("codex_home"))
            else:
                if (not isinstance(capabilities, dict) or capabilities.get("profile") != "routine_development_v1"
                        or capabilities.get("network") is not False or capabilities.get("external_credentials") is not False
                        or capabilities.get("git_metadata") != "read_only" or not isinstance(capabilities.get("writable_paths"), list)):
                    raise ValueError("invalid routine execution capabilities")
                routine_tmp = workspace / ".orchestra-tmp"; routine_tmp.mkdir(mode=0o700)
                command = routine_command(executable, workspace, result, capabilities["writable_paths"])
                environment = worker_environment(spec.get("codex_home")); environment["TMPDIR"] = str(routine_tmp)
            with prompt.open("rb") as input_file:
                output = subprocess.DEVNULL if adapter == "opencode" else log.open("ab")
                try:
                    exit_code = subprocess.run(command, cwd=workspace, env=environment, stdin=input_file,
                        stdout=output, stderr=subprocess.STDOUT, check=False, timeout=seconds).returncode
                finally:
                    if output is not subprocess.DEVNULL: output.close()
    except subprocess.TimeoutExpired:
        exit_code, error_type = 124, "WorkerTimeout"
    except RunnerTerminated as exc:
        exit_code, error_type = 128 + exc.signum, type(exc).__name__
    except BaseException as exc:
        error_type = type(exc).__name__; raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        if routine_tmp is not None:
            shutil.rmtree(routine_tmp, ignore_errors=True)
            if routine_tmp.exists():
                exit_code, error_type = 125, "RoutineTemporaryDirectoryCleanupError"
        if runtime_dir is not None:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            if runtime_dir.exists():
                exit_code, error_type = 125, "OpenCodeRuntimeDirectoryCleanupError"
            elif not result.exists():
                write_private_text(result, "OpenCode execution completed; provider output is intentionally not retained.\n"
                    if exit_code == 0 else f"OpenCode execution failed (exit {exit_code}); provider output is intentionally not retained.\n")
        atomic_json(completion, {"exit_code": exit_code, "finished_at": utc_now(), "runner_error": error_type})
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one OMCP worker safely")
    parser.add_argument("--spec", type=Path, required=True)
    return run(parser.parse_args(argv).spec)


if __name__ == "__main__":
    raise SystemExit(main())
