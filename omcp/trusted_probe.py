"""Runner-controlled trusted security verification for routine workers.

The probe implementation and its report live under coordinator-owned state,
outside every path writable by the Codex worker.  The coordinator launches a
separate transient systemd user service with the same confinement properties as
the routine worker and runs the probe inside a Bubblewrap sandbox that denies
the asserted operations.  No part of this evidence is writable by Codex.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from omcp import worker_contract


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


def _prepare_probe_root(state_dir: Path, task_id: str) -> tuple[Path, Path, Path, Path]:
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise TrustedProbeError("invalid task id for security probe")
    probe_root = Path(state_dir) / "security-probes" / task_id
    probe_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(probe_root, 0o700)
    probe_script = probe_root / "orchestra-security-probe.py"
    source = Path(__file__).with_name("routine_probe.py")
    _write_private(probe_script, source.read_bytes())
    os.chmod(probe_script, 0o600)
    deny_root = probe_root / "deny"
    deny_root.mkdir(mode=0o700, exist_ok=True)
    deny_config = deny_root / "omcp.json"
    deny_secret = deny_root / "deepseek-api-key"
    for path in (deny_config, deny_secret):
        if not path.exists():
            path.write_bytes(b"")
        os.chmod(path, 0)
    report = probe_root / "orchestra-security-probe.json"
    return probe_root, probe_script, deny_config, deny_secret


def _build_command(
    *, probe_root: Path, probe_script: Path, workspace: Path,
    deny_config: Path, deny_secret: Path, report: Path,
    host_config: Path, host_secret: Path, unit: str,
) -> tuple[list[str], Path]:
    bwrap = shutil.which("bwrap")
    systemd_run = shutil.which("systemd-run")
    if not bwrap or not systemd_run:
        raise TrustedProbeError("bwrap and systemd-run are required for trusted verification")
    for path in (host_config, host_secret):
        if not path.is_file():
            raise TrustedProbeError("owner-only security probe input is unavailable")
    runtime_dir = _runtime_dir()
    sandbox = [
        bwrap, "--die-with-parent", "--ro-bind", "/", "/",
        "--dev", "/dev", "--proc", "/proc", "--unshare-net",
        "--bind", str(probe_root), str(probe_root),
        "--ro-bind", str(workspace), str(workspace),
        "--ro-bind", str(deny_config), str(host_config),
        "--ro-bind", str(deny_secret), str(host_secret),
        "--", sys.executable, str(probe_script),
        "--worktree", str(workspace), "--report", str(report),
        "--orchestra-config", str(host_config),
        "--orchestra-secret", str(host_secret),
    ]
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
        f"ReadWritePaths={probe_root}",
    ]
    command = [
        systemd_run, "--user", "--wait", "--pipe", "--collect", "--quiet",
        f"--unit={unit}",
        *[f"--property={value}" for value in properties],
        "--setenv=PATH=/usr/local/bin:/usr/bin:/bin",
        "--setenv=PYTHONDONTWRITEBYTECODE=1",
        "--", *sandbox,
    ]
    return command, runtime_dir


def run_trusted_security_probe(
    workspace: Path, state_dir: Path, task_id: str, *,
    host_config: Path | None = None, host_secret: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Run all five negative assertions and return validated trusted evidence."""
    workspace = Path(workspace)
    if not workspace.is_dir() or workspace.is_symlink():
        raise TrustedProbeError("task workspace is unavailable")
    host_config = Path(host_config) if host_config is not None else HOST_CONFIG
    host_secret = Path(host_secret) if host_secret is not None else HOST_SECRET
    probe_root, probe_script, deny_config, deny_secret = _prepare_probe_root(
        Path(state_dir), task_id,
    )
    report = probe_root / "orchestra-security-probe.json"
    if report.exists():
        return worker_contract.read_security_report(report)
    unit = "orchestra-security-" + task_id
    command, runtime_dir = _build_command(
        probe_root=probe_root, probe_script=probe_script, workspace=workspace,
        deny_config=deny_config, deny_secret=deny_secret, report=report,
        host_config=host_config, host_secret=host_secret, unit=unit,
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
    if not report.exists():
        raise TrustedProbeError("trusted security probe report is missing")
    checks = worker_contract.read_security_report(report)
    if len(checks) != len(worker_contract.REQUIRED_SECURITY_CHECKS):
        raise TrustedProbeError("trusted security probe evidence is incomplete")
    return checks
