"""Trusted, coordinator-controlled routine security verification.

The probe implementation and report live under coordinator-owned state.
Execution uses Codex's own ``sandbox`` command with the same authoritative
routine-confinement policy used by the real worker, so a weakened worker policy
cannot be masked by an independently stricter probe sandbox.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from omcp import worker_contract
from omcp.routine_policy import RoutineConfinementPolicy


HOST_CONFIG = Path("/etc/orchestra/omcp.json")
HOST_SECRET = Path("/etc/orchestra/secrets/deepseek-api-key")
TASK_ID = re.compile(r"[0-9a-f-]{36}")


class TrustedProbeError(RuntimeError):
    """Trusted verification could not be executed or was invalid."""


def _runtime_dir() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    root = Path(configured) if configured else Path(f"/run/user/{os.getuid()}")
    if not root.is_dir() or not (root / "systemd" / "private").is_socket():
        raise TrustedProbeError("systemd user manager is unavailable")
    return root


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def prepare_probe_root(state_dir: Path, task_id: str) -> tuple[Path, Path, Path]:
    """Create the coordinator-owned probe script and private Codex home."""
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise TrustedProbeError("invalid task id for security probe")
    probe_root = Path(state_dir) / "security-probes" / task_id
    probe_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(probe_root, 0o700)
    probe_script = probe_root / "orchestra-security-probe.py"
    source = Path(__file__).with_name("routine_probe.py")
    _write_private(probe_script, source.read_bytes())
    codex_home = probe_root / "codex-home"
    codex_home.mkdir(mode=0o700, exist_ok=True)
    os.chmod(codex_home, 0o700)
    return probe_root, probe_script, codex_home


def _build_command(
    *, policy: RoutineConfinementPolicy, workspace: Path, probe_script: Path,
    codex_home: Path, unit: str, read_write_paths: tuple[Path, ...],
) -> tuple[list[str], Path]:
    codex = shutil.which("codex")
    systemd_run = shutil.which("systemd-run")
    if not codex or not systemd_run:
        raise TrustedProbeError("codex and systemd-run are required for trusted verification")
    for path in (HOST_CONFIG, HOST_SECRET):
        if not path.is_file() or path.is_symlink():
            raise TrustedProbeError("owner-only security probe input is unavailable")
    runtime_dir = _runtime_dir()
    probe_command = [
        sys.executable, str(probe_script),
        "--worktree", str(workspace), "--report", "-",
        "--orchestra-config", str(HOST_CONFIG),
        "--orchestra-secret", str(HOST_SECRET),
    ]
    sandbox = policy.probe_command(codex, workspace, probe_command)
    properties = [
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "PrivateTmp=yes",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=no",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "UMask=0077",
        "KillMode=control-group",
        "TimeoutStopSec=30",
        f"ReadWritePaths={codex_home}",
        *[f"ReadWritePaths={path}" for path in read_write_paths],
    ]
    command = [
        systemd_run, "--user", "--wait", "--pipe", "--collect", "--quiet",
        f"--unit={unit}",
        *[f"--property={value}" for value in properties],
        f"--setenv=HOME={codex_home}",
        f"--setenv=CODEX_HOME={codex_home}",
        "--setenv=PATH=/usr/local/bin:/usr/bin:/bin",
        "--setenv=PYTHONDONTWRITEBYTECODE=1",
        "--", *sandbox,
    ]
    return command, runtime_dir


def _parse_trusted_report(stdout: str) -> dict[str, Any]:
    for line in reversed([line.strip() for line in stdout.splitlines() if line.strip()]):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(value, dict) and value.get("schema_version") == 1
                and isinstance(value.get("checks"), list)):
            return value
    raise TrustedProbeError("trusted security probe did not emit a valid report")


def run_trusted_security_probe(
    workspace: Path, state_dir: Path, task_id: str,
    writable_paths: tuple[str, ...], read_only_paths: tuple[str, ...],
    read_write_paths: tuple[str, ...] = (), *,
    policy: RoutineConfinementPolicy | None = None,
) -> dict[str, dict[str, Any]]:
    """Run the probe under the exact routine policy and validate its evidence."""
    workspace = Path(workspace)
    if not workspace.is_dir() or workspace.is_symlink():
        raise TrustedProbeError("task workspace is unavailable")
    probe_root, probe_script, codex_home = prepare_probe_root(Path(state_dir), task_id)
    if policy is None:
        policy = RoutineConfinementPolicy(
            tuple(writable_paths),
            tuple(dict.fromkeys((*read_only_paths, str(probe_root)))),
        )
    elif str(probe_root) not in policy.read_only_paths:
        raise TrustedProbeError("trusted probe root is not in the shared policy")
    report = probe_root / "orchestra-security-probe.json"
    if report.exists():
        return worker_contract.read_security_report(report)
    unit = "orchestra-security-" + task_id
    write_paths = tuple(dict.fromkeys((str(workspace), *read_write_paths)))
    command, runtime_dir = _build_command(
        policy=policy, workspace=workspace, probe_script=probe_script,
        codex_home=codex_home, unit=unit,
        read_write_paths=tuple(Path(value) for value in write_paths),
    )
    environment = {**os.environ, "XDG_RUNTIME_DIR": str(runtime_dir)}
    try:
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=False, timeout=90,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        subprocess.run(
            ["systemctl", "--user", "stop", unit], env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=10,
        )
        raise TrustedProbeError("trusted security probe timed out") from exc
    if completed.returncode != 0:
        raise TrustedProbeError("trusted security probe failed")
    report_value = _parse_trusted_report(completed.stdout)
    _write_private(report, (json.dumps(report_value, sort_keys=True) + "\n").encode())
    checks = worker_contract.read_security_report(report)
    if len(checks) != len(worker_contract.REQUIRED_SECURITY_CHECKS):
        raise TrustedProbeError("trusted security probe evidence is incomplete")
    return checks
