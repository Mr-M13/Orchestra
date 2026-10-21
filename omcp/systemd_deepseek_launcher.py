#!/usr/bin/python3
"""Root-owned, fixed-function launcher for one DeepSeek worker.

This is intentionally not a general systemd wrapper.  The unprivileged
coordinator may select only an existing task UUID; this program derives every
command, path, identity and network rule itself.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import pwd
import re
import signal
import stat
import subprocess
import sys
from pathlib import Path

STATE_ROOT = Path("/home/hamed/orchestra-migration/state")
RELEASE_ROOT = Path("/opt/orchestra/current")
CREDENTIAL = Path("/etc/orchestra/secrets/deepseek-api-key")
POLICY = Path("/etc/orchestra/deepseek-worker.json")
TASK_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")


def fail(message: str) -> None:
    raise SystemExit(f"orchestra DeepSeek launcher: {message}")


def private_regular(path: Path, owner: int) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        fail(f"unsafe file: {path}")
    if info.st_uid != owner or info.st_mode & 0o077:
        fail(f"file is not private and owned by the expected identity: {path}")


def under(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError:
        fail(f"path escapes approved root: {path}")
    return resolved


def policy_addresses() -> tuple[str, ...]:
    private_regular(POLICY, 0)
    try:
        raw = json.loads(POLICY.read_text())
        values = raw["endpoint_addresses"]
    except (OSError, ValueError, KeyError, TypeError):
        fail("invalid root-owned DeepSeek network policy")
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        fail("network policy requires one to 32 endpoint addresses")
    try:
        return tuple(str(ipaddress.ip_address(value)) for value in values)
    except ValueError:
        fail("network policy contains a non-literal IP address")


def task_spec(task_id: str) -> tuple[Path, Path, Path]:
    task_dir = STATE_ROOT / "tasks" / task_id
    spec = task_dir / "runner.json"
    private_regular(spec, pwd.getpwnam("hamed").pw_uid)
    try:
        raw = json.loads(spec.read_text())
    except (OSError, ValueError):
        fail("invalid task specification")
    if raw.get("adapter") != "deepseek" or raw.get("task_id") != task_id:
        fail("task is not an exact DeepSeek task")
    if raw.get("credential_file") != str(CREDENTIAL):
        fail("task credential path is not approved")
    workspace = under(Path(raw.get("workspace", "")), STATE_ROOT / "worktrees")
    if workspace.name != task_id:
        fail("workspace is not bound to task identity")
    for name in ("prompt_path", "result_path", "completion_path", "log_path"):
        if under(Path(raw.get(name, "")), task_dir) != task_dir / Path(raw[name]).name:
            fail(f"task {name} is not a direct private task file")
    return spec.resolve(strict=True), workspace, task_dir.resolve(strict=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        fail("must run as root through the reviewed sudo rule")
    if not TASK_ID.fullmatch(args.task_id):
        fail("invalid task ID")
    if not RELEASE_ROOT.is_dir() or not CREDENTIAL.is_file():
        fail("required release or credential is unavailable")
    worker = pwd.getpwnam("orchestra-worker")
    credential_info = CREDENTIAL.stat()
    if (credential_info.st_uid != worker.pw_uid or credential_info.st_mode & 0o077
            or not stat.S_ISREG(credential_info.st_mode)):
        fail("credential is not private to orchestra-worker")
    spec, workspace, task_dir = task_spec(args.task_id)
    # The coordinator remains the directory owner so it can collect bounded
    # completion artifacts.  Only this task directory is shared with the
    # worker group; all other coordinator state stays inaccessible.
    os.chown(task_dir, -1, worker.pw_gid)
    os.chmod(task_dir, 0o770)
    for name in ("runner.json", "prompt.txt"):
        path = task_dir / name
        os.chown(path, -1, worker.pw_gid)
        os.chmod(path, 0o640)
    addresses = policy_addresses()
    unit = "orchestra-deepseek-" + args.task_id
    command = [
        "/usr/bin/systemd-run", "--wait", "--collect", "--quiet", "--pipe",
        f"--unit={unit}", "--service-type=exec",
        "--uid=orchestra-worker", "--gid=orchestra-worker",
        "--property=NoNewPrivileges=yes", "--property=PrivateTmp=yes",
        "--property=ProtectSystem=strict", "--property=ProtectHome=tmpfs",
        "--property=ProtectKernelTunables=yes", "--property=ProtectControlGroups=yes",
        "--property=ProtectKernelModules=yes", "--property=RestrictSUIDSGID=yes",
        "--property=LockPersonality=yes", "--property=RestrictNamespaces=yes",
        "--property=CapabilityBoundingSet=", "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "--property=IPAddressDeny=any",
        f"--property=BindReadOnlyPaths={RELEASE_ROOT}:{RELEASE_ROOT}",
        f"--property=BindReadOnlyPaths={CREDENTIAL}:{CREDENTIAL}",
        f"--property=BindPaths={workspace}:{workspace}",
        f"--property=BindPaths={task_dir}:{task_dir}",
        f"--working-directory={RELEASE_ROOT}",
        "--setenv=PATH=/usr/local/bin:/usr/bin:/bin", "--setenv=HOME=/tmp", "--setenv=TMPDIR=/tmp",
    ]
    for address in addresses:
        command.append(f"--property=IPAddressAllow={address}{'/128' if ':' in address else '/32'}")
    command += ["--", "/usr/bin/python3", "-m", "omcp.runner", "--spec", str(spec)]
    child = subprocess.Popen(command)
    def stop(_signum: int, _frame: object) -> None:
        subprocess.run(["/usr/bin/systemctl", "stop", unit], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child.wait(timeout=10)
        raise SystemExit(128 + _signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
