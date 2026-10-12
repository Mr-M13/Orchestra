from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omcp.controller import canonical_codex_executable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def worker_environment(codex_home: str | None) -> dict[str, str]:
    """Build a small environment for Codex without forwarding tunnel credentials."""
    permitted = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME")
    environment = {key: os.environ[key] for key in permitted if key in os.environ}
    if codex_home:
        environment["CODEX_HOME"] = codex_home
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = (json.dumps(value, separators=(",", ":")) + "\n").encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(data)
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


def routine_command(executable: str, workspace: Path, result_path: Path,
                    writable_paths: list[str]) -> list[str]:
    """Build a fail-closed Codex permission profile for standing-authorized work."""
    if not all(
        isinstance(value, str) and value
        and all(part and part.replace("_", "").replace("-", "").isalnum()
                for part in Path(value).parts)
        for value in writable_paths
    ):
        raise ValueError("invalid routine writable paths")
    workspace_rules = ','.join([
        '"."="read"',
        '".orchestra-tmp"="write"',
        *(f'"{value}"="write"' for value in writable_paths),
    ])
    command = [
        executable, "--ask-for-approval", "never", "exec",
        "--ignore-user-config", "--ignore-rules", "--ephemeral",
        "--config", 'default_permissions="orchestra-routine"',
        "--config", 'permissions.orchestra-routine.filesystem.:root="deny"',
        "--config", 'permissions.orchestra-routine.filesystem.:minimal="read"',
        "--config", 'permissions.orchestra-routine.filesystem.:tmpdir="deny"',
        "--config", 'permissions.orchestra-routine.filesystem.:slash_tmp="deny"',
        "--config", (
            "permissions.orchestra-routine.filesystem.:workspace_roots={"
            + workspace_rules + "}"
        ),
        "--config", "permissions.orchestra-routine.network.enabled=false",
    ]
    return command + [
        "--cd", str(workspace), "--output-last-message", str(result_path), "-",
    ]


def run(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text())
    prompt_path = Path(spec["prompt_path"])
    log_path = Path(spec["log_path"])
    completion_path = Path(spec["completion_path"])
    result_path = Path(spec["result_path"])
    workspace = Path(spec["workspace"])
    configured = spec.get("command")
    if not isinstance(configured, list) or len(configured) != 1:
        raise ValueError("runner spec must contain exactly one Codex executable")
    executable = canonical_codex_executable(configured[0])
    capabilities = spec.get("execution_capabilities")
    routine_tmp: Path | None = None
    if capabilities is None:
        command = [executable, "--ask-for-approval", "never", "exec",
                   "--sandbox", "workspace-write", "--cd", str(workspace),
                   "--output-last-message", str(result_path), "-"]
    else:
        if (not isinstance(capabilities, dict)
                or capabilities.get("profile") != "routine_development_v1"
                or capabilities.get("network") is not False
                or capabilities.get("external_credentials") is not False
                or capabilities.get("git_metadata") != "read_only"
                or not isinstance(capabilities.get("writable_paths"), list)):
            raise ValueError("invalid routine execution capabilities")
        writable_paths = capabilities["writable_paths"]
        command = routine_command(executable, workspace, result_path, writable_paths)
    environment = worker_environment(spec.get("codex_home"))
    if capabilities is not None:
        routine_tmp = workspace / ".orchestra-tmp"
        routine_tmp.mkdir(mode=0o700)
        environment["TMPDIR"] = str(routine_tmp)
    exit_code = 125
    error_type = None
    try:
        with prompt_path.open("rb") as prompt, log_path.open("ab") as log:
            exit_code = subprocess.run(
                command, cwd=workspace, env=environment, stdin=prompt,
                stdout=log, stderr=subprocess.STDOUT, check=False,
            ).returncode
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        if routine_tmp is not None:
            shutil.rmtree(routine_tmp, ignore_errors=True)
            if routine_tmp.exists():
                exit_code = 125
                error_type = "RoutineTemporaryDirectoryCleanupError"
        atomic_json(completion_path, {
            "exit_code": exit_code,
            "finished_at": utc_now(),
            "runner_error": error_type,
        })
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one OMCP worker safely")
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args(argv)
    return run(args.spec)


if __name__ == "__main__":
    raise SystemExit(main())
