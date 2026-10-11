from __future__ import annotations

import argparse
import json
import os
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
    command = [executable, "--ask-for-approval", "never", "exec",
               "--sandbox", "workspace-write", "--cd", str(workspace),
               "--output-last-message", str(result_path), "-"]
    environment = worker_environment(spec.get("codex_home"))
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
