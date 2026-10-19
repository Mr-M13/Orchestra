"""Run owner-enabled OMCP host operations without exposing an arbitrary shell."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omcp.runner import RunnerTerminated, atomic_json, write_startup_lease


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _free_loopback_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def _write_docker_wrapper(path: Path) -> None:
    source_root = Path(__file__).resolve().parent.parent
    data = (
        f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(source_root)!r})\n"
        "from omcp.docker_gate import main\nraise SystemExit(main())\n"
    ).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_path(value: Any, parent: Path | None = None) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("host operation path is invalid")
    path = Path(value).resolve(strict=False)
    if parent is not None and path != parent and not path.is_relative_to(parent):
        raise ValueError("host operation path escapes its task directory")
    return path


def run(spec_path: Path) -> int:
    spec_path = spec_path.resolve(strict=True)
    spec = json.loads(spec_path.read_text())
    write_startup_lease(spec_path, spec)
    if spec.get("operation_type") != "delnavazan_mariadb_acceptance_v1":
        raise ValueError("unsupported host operation type")
    operation_id = spec.get("operation_id")
    target_commit = spec.get("target_commit")
    timeout = spec.get("timeout_seconds")
    if (not isinstance(operation_id, str) or not operation_id
            or not isinstance(target_commit, str) or len(target_commit) != 40
            or any(character not in "0123456789abcdef" for character in target_commit)
            or not isinstance(timeout, int) or isinstance(timeout, bool) or not 60 <= timeout <= 7200):
        raise ValueError("invalid host operation specification")
    task_dir = _safe_path(spec.get("task_dir"))
    if spec_path.parent != task_dir:
        raise ValueError("host operation specification is outside its task directory")
    workspace = _safe_path(spec.get("workspace"))
    log_path = _safe_path(spec.get("log_path"), task_dir)
    result_path = _safe_path(spec.get("result_path"), task_dir)
    completion_path = _safe_path(spec.get("completion_path"), task_dir)
    run_script = (workspace / "platform/runtime/bin/run-all.sh").resolve(strict=True)
    destroy_script = (workspace / "platform/runtime/bin/destroy.sh").resolve(strict=True)
    runtime_dir = (workspace / "platform/runtime").resolve(strict=True)
    for path in (run_script, destroy_script, runtime_dir):
        if not path.is_relative_to(workspace):
            raise ValueError("acceptance harness escapes the approved worktree")
    real_docker = shutil.which("docker")
    if not real_docker:
        raise RuntimeError("Docker CLI is unavailable")
    real_docker = str(Path(real_docker).resolve(strict=True))
    runtime_state = task_dir / "runtime-state"
    mount_stage = runtime_state / "staged-mounts"
    gate_bin = task_dir / "docker-gate-bin"
    gate_bin.mkdir(mode=0o700)
    runtime_state.mkdir(mode=0o700)
    mount_stage.mkdir(mode=0o700)
    _write_docker_wrapper(gate_bin / "docker")
    override = task_dir / "compose.internal.yaml"
    override.write_text("networks:\n  runtime:\n    internal: true\n")
    os.chmod(override, 0o600)
    project = "omcp-dzn-" + spec_path.parent.name.split("-", 1)[0]
    private_home = task_dir / "home"
    private_home.mkdir(mode=0o700)
    environment = {
        key: os.environ[key] for key in ("LANG", "LC_ALL") if key in os.environ
    }
    environment.update({
        "HOME": str(private_home),
        "PATH": str(gate_bin) + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(task_dir / "tmp"),
        "GIT_TERMINAL_PROMPT": "0",
        "DZN_RUNTIME_STATE_DIR": str(runtime_state),
        "DZN_COMPOSE_PROJECT": project,
        "DZN_TARGET_REF": target_commit,
        "DZN_WP_PORT": str(_free_loopback_port()),
        "OMCP_DOCKER_REAL": real_docker,
        "OMCP_DOCKER_PROJECT_DIR": str(runtime_dir),
        "OMCP_DOCKER_PROJECT": project,
        "OMCP_DOCKER_COMPOSE_FILE": str(runtime_dir / "compose.yaml"),
        "OMCP_DOCKER_OVERRIDE": str(override),
        "OMCP_DOCKER_RUNTIME_STATE": str(runtime_state),
        "OMCP_DOCKER_NETWORK": project + "_runtime",
        "OMCP_DOCKER_APPROVED_SOURCE": str(workspace),
        "OMCP_DOCKER_MOUNT_STAGE": str(mount_stage),
    })
    original_home = os.environ.get("HOME")
    socket_candidates = [Path("/var/run/docker.sock")]
    if original_home:
        socket_candidates.insert(0, Path(original_home) / ".docker/run/docker.sock")
    docker_socket = next((candidate for candidate in socket_candidates if candidate.exists()), None)
    if docker_socket is not None:
        environment["DOCKER_HOST"] = "unix://" + str(docker_socket.resolve(strict=True))
    Path(environment["TMPDIR"]).mkdir(mode=0o700)
    exit_code = 125
    error_type = None
    cleanup_code = None
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def terminate(signum: int, _frame: Any) -> None:
        raise RunnerTerminated(signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        with log_path.open("ab") as log:
            try:
                exit_code = subprocess.run(
                    ["/bin/bash", str(run_script)], cwd=workspace, env=environment,
                    stdout=log, stderr=subprocess.STDOUT, check=False, timeout=timeout,
                ).returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
                error_type = "HostOperationTimeout"
            finally:
                mount_cleanup_failed = False
                try:
                    shutil.rmtree(mount_stage)
                except OSError as exc:
                    mount_cleanup_failed = True
                    print(f"failed to remove Docker mount staging: {exc}", file=log)
                try:
                    cleanup_code = subprocess.run(
                        ["/bin/bash", str(destroy_script), "YES"], cwd=workspace,
                        env=environment, stdout=log, stderr=subprocess.STDOUT,
                        check=False, timeout=min(300, timeout),
                    ).returncode
                except subprocess.TimeoutExpired:
                    cleanup_code = 124
                if mount_cleanup_failed and cleanup_code == 0:
                    cleanup_code = 125
                if cleanup_code and exit_code == 0:
                    exit_code = 125
                    error_type = "HostOperationCleanupError"
    except RunnerTerminated as exc:
        exit_code = 128 + exc.signum
        error_type = type(exc).__name__
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        result_path.write_text(json.dumps({
            "operation_id": operation_id, "target_commit": target_commit,
            "exit_code": exit_code, "cleanup_exit_code": cleanup_code,
            "log": log_path.name,
        }, sort_keys=True) + "\n")
        os.chmod(result_path, 0o600)
        atomic_json(completion_path, {
            "exit_code": exit_code, "finished_at": utc_now(),
            "runner_error": error_type,
        })
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one approved OMCP host operation")
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args(argv)
    return run(args.spec)


if __name__ == "__main__":
    raise SystemExit(main())
