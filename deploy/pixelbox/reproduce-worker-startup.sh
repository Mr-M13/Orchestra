#!/usr/bin/env bash
# Credit-free reproduction of the direct-provider Bubblewrap startup failure.
#
# Builds the same Bubblewrap policy that omcp.controller.deepseek_runner_command
# uses, but executes /usr/bin/true instead of the Python runner.  No provider
# request is made and no credential content is read or printed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
CONFIG="${ORCHESTRA_CONFIG:-/etc/orchestra/omcp.json}"

usage() {
  cat <<'EOF'
Usage: reproduce-worker-startup.sh [--config <path>]

Runs the reviewed DeepSeek Bubblewrap policy twice through transient systemd
user services: once with the coordinator's RestrictSUIDSGID=yes filter and once
with the launcher's RestrictSUIDSGID=no override.  The probe command is
/usr/bin/true and does not contact api.deepseek.com.
EOF
}

while (($#)); do
  case "$1" in
    --config)
      shift
      (($#)) || { echo "--config requires a value" >&2; exit 2; }
      CONFIG="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[[ "$(uname -s)" == "Linux" ]] || {
  echo "This reproduction requires the Linux PixelBox worker host." >&2
  exit 2
}
command -v systemd-run >/dev/null || {
  echo "systemd-run is required for the user-service launch context." >&2
  exit 2
}
[[ -r "$CONFIG" ]] || {
  echo "configuration is not readable: $CONFIG" >&2
  exit 2
}

PYTHONDONTWRITEBYTECODE=1 python3 - "$ROOT" "$CONFIG" <<'PY'
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1]).resolve()
config_path = Path(sys.argv[2])
config = json.loads(config_path.read_text(encoding="utf-8"))

workers = config.get("workers")
if not isinstance(workers, dict):
    raise SystemExit("configuration has no workers object")
deepseek = next(
    (value for value in workers.values()
     if isinstance(value, dict) and value.get("adapter") == "deepseek"),
    None,
)
if deepseek is None:
    raise SystemExit("configuration has no direct DeepSeek worker to reproduce")
credential_raw = deepseek.get("credential_file")
if not isinstance(credential_raw, str) or not credential_raw:
    raise SystemExit("direct DeepSeek worker has no credential_file")
credential = Path(credential_raw).expanduser()
state_root = Path(config.get("state_dir", "/tmp")).expanduser()
source_root = root

# Metadata only: never print credential contents.
try:
    stat = credential.stat()
except OSError:
    print(f"credential: missing at configured path {credential}")
    raise SystemExit(1)
print(f"credential path: {credential}")
print(f"credential owner-only: {stat.st_mode & 0o077 == 0 and stat.st_uid == os.getuid()}")
if not credential.is_file() or stat.st_mode & 0o077 or stat.st_uid != os.getuid():
    raise SystemExit("credential must be an owner-only regular file")

true = shutil.which("true")
bwrap = shutil.which("bwrap")
systemd_run = shutil.which("systemd-run")
if not true or not bwrap or not systemd_run:
    raise SystemExit("bwrap, systemd-run and true are required")

state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
probe_root = Path(tempfile.mkdtemp(prefix="startup-probe-", dir=state_root))
task_dir = probe_root / "task"
workspace = probe_root / "workspace"
task_dir.mkdir(mode=0o700)
workspace.mkdir(mode=0o700)

sys.path.insert(0, str(root))
from omcp.platform import SandboxPolicy, sandbox_command  # noqa: E402

policy = SandboxPolicy(
    read_paths=(source_root, workspace, task_dir),
    read_files=(credential,),
    write_paths=(task_dir,),
    # The provider host is present exactly as in the real DeepSeek policy.  The
    # probe executable opens no socket, so this consumes no credits and sends
    # no request.
    network_hosts=("api.deepseek.com",),
    backend="bubblewrap",
)
command = sandbox_command(policy, [true])

base_properties = [
    f"WorkingDirectory={source_root}",
    "ProtectSystem=strict",
    "ProtectHome=read-only",
    "PrivateTmp=yes",
    "NoNewPrivileges=yes",
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
    "LockPersonality=yes",
    "RestrictRealtime=yes",
    "UMask=0077",
    "KillMode=control-group",
    "TimeoutStopSec=60",
    f"ReadWritePaths={task_dir}",
    f"ReadWritePaths={workspace}",
]
environment = {
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", str(Path.home())),
    "GIT_TERMINAL_PROMPT": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
}
runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
environment["XDG_RUNTIME_DIR"] = runtime

def sanitize(value: bytes) -> str:
    text = value.decode("utf-8", "replace")
    text = "".join(char if char in "\n\t" or ord(char) >= 0x20 else " " for char in text)
    text = re.sub(r"(?i)(bearer|authorization|token|secret|api[_-]?key)\s*[:=]?\s*\S+", "[redacted]", text)
    return text[:1500]

def run(label: str, restrict_suid_sgid: str) -> int:
    properties = [f"RestrictSUIDSGID={restrict_suid_sgid}", *base_properties]
    argv = [
        systemd_run, "--user", "--wait", "--collect", "--quiet", "--pipe",
        f"--unit=orchestra-startup-probe-{os.getpid()}-{label}",
        *[f"--property={value}" for value in properties],
        *[f"--setenv={key}={value}" for key, value in sorted(environment.items())],
        "--", *command,
    ]
    try:
        completed = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, check=False, timeout=60,
            env={**os.environ, "XDG_RUNTIME_DIR": runtime},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"{label}: launch error type={type(exc).__name__}")
        return 125
    print(f"{label}: RestrictSUIDSGID={restrict_suid_sgid} exit={completed.returncode}")
    detail = sanitize(completed.stderr)
    if detail.strip():
        print(f"{label}: sanitized stderr (bounded):")
        print(detail)
    return completed.returncode

try:
    restricted = run("coordinator-filter", "yes")
    relaxed = run("user-service-launcher", "no")
finally:
    shutil.rmtree(probe_root, ignore_errors=True)

if relaxed != 0:
    print("RESULT: FAILED - the user-service launcher could not start Bubblewrap")
    raise SystemExit(1)
if restricted != 0:
    print("RESULT: REPRODUCED - the coordinator filter breaks Bubblewrap and the user service starts it")
else:
    print("RESULT: NOT_REPRODUCED - this host started Bubblewrap under both filters")
PY
