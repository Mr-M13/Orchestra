#!/usr/bin/env python3
"""Trusted routine-security probe executed in a coordinator-controlled sandbox.

The coordinator launches this implementation in a separate transient service
under the shared Codex routine sandbox policy and stores its report outside
every Codex-writable path.  The probe attempts each required negative security
action exactly once and emits structured evidence.  It never reads or prints protected contents.  Missing or ambiguous
evidence fails closed: unexpected exceptions are recorded as allowed so the
coordinator rejects the task.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any


def _check(name: str, attempted: bool, allowed: bool, error_type: str,
           command: str) -> dict[str, Any]:
    return {
        "name": name,
        "attempted": attempted,
        "allowed": allowed,
        "error_type": error_type,
        "command": command,
    }


def _read_attempt(name: str, path: Path) -> dict[str, Any]:
    command = f'open("{path}", "rb").read(1)'
    try:
        with path.open("rb") as source:
            source.read(1)
    except OSError as exc:
        return _check(name, True, False, type(exc).__name__, command)
    except Exception as exc:  # ambiguous evidence must fail closed
        return _check(name, True, True, "Unexpected" + type(exc).__name__, command)
    return _check(name, True, True, "ReadSucceeded", command)


def _write_attempt(name: str, target: Path) -> dict[str, Any]:
    command = f'open("{target}", "xb").write(...)'
    try:
        with target.open("xb") as output:
            output.write(b"orchestra routine probe\n")
    except OSError as exc:
        return _check(name, True, False, type(exc).__name__, command)
    except Exception as exc:
        return _check(name, True, True, "Unexpected" + type(exc).__name__, command)
    try:
        target.unlink()
    except OSError:
        pass
    return _check(name, True, True, "WriteSucceeded", command)


def _network_attempt(host: str, port: int) -> dict[str, Any]:
    command = f'socket.create_connection(("{host}", {port}), timeout=3)'
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError as exc:
        return _check("network_egress", True, False, type(exc).__name__, command)
    except Exception as exc:
        return _check("network_egress", True, True, "Unexpected" + type(exc).__name__, command)
    return _check("network_egress", True, True, "ConnectionSucceeded", command)


def _git_metadata_attempt(worktree: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _check(
            "git_metadata_write", True, False,
            "GitMetadataUnavailable" + type(exc).__name__,
            "git rev-parse --git-dir",
        )
    if result.returncode != 0:
        return _check(
            "git_metadata_write", True, False,
            "GitMetadataUnavailable",
            "git rev-parse --git-dir",
        )
    raw = result.stdout.strip()
    if not raw:
        return _check(
            "git_metadata_write", True, False,
            "GitMetadataUnavailable",
            "git rev-parse --git-dir",
        )
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        git_dir = worktree / git_dir
    target = git_dir / ("ORCHESTRA-ROUTINE-PROBE-" + str(os.getpid()))
    return _write_attempt("git_metadata_write", target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Attempt routine security assertions")
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--report", type=str, required=True)
    parser.add_argument("--orchestra-config", type=Path,
                        default=Path("/etc/orchestra/omcp.json"))
    parser.add_argument("--orchestra-secret", type=Path,
                        default=Path("/etc/orchestra/secrets/deepseek-api-key"))
    parser.add_argument("--outside-target", type=Path,
                        default=Path("/tmp/orchestra-routine-probe-" + uuid.uuid4().hex + ".txt"))
    parser.add_argument("--network-host", default="example.com")
    parser.add_argument("--network-port", type=int, default=443)
    args = parser.parse_args(argv)

    worktree = args.worktree.expanduser().resolve(strict=True)

    checks = [
        _read_attempt("read_orchestra_config", args.orchestra_config),
        _read_attempt("read_orchestra_secret", args.orchestra_secret),
        _write_attempt("write_outside_worktree", args.outside_target),
        _network_attempt(args.network_host, args.network_port),
        _git_metadata_attempt(worktree),
    ]
    report = {
        "schema_version": 1,
        "worktree": str(worktree),
        "checks": checks,
    }
    payload = json.dumps(report, sort_keys=True) + "\n"
    if args.report == "-":
        sys.stdout.write(payload)
        sys.stdout.flush()
        return 0
    report_path = Path(args.report).expanduser().resolve()
    try:
        report_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with report_path.open("x", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(report_path, 0o600)
    except OSError as exc:
        print(f"security probe could not write its report: {type(exc).__name__}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
