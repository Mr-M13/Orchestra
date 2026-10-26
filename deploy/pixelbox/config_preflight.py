#!/usr/bin/env python3
"""Fail-closed activation preflight for the owner-only coordinator config."""

from __future__ import annotations

import argparse
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_MIN_FREE = 512 * 1024 * 1024
REQUIRED_RELEASE_FILES = (
    "omcp/server.py",
    "omcp/health.py",
    "deploy/pixelbox/install.sh",
    "deploy/pixelbox/verify.sh",
    "deploy/pixelbox/systemd/orchestra-omcp.service",
    "deploy/pixelbox/systemd/orchestra-health.service",
)


class PreflightError(ValueError):
    pass


def _fail(message: str) -> None:
    raise PreflightError(message)


def _private_file(path: Path) -> None:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise PreflightError("coordinator config is unavailable") from exc
    if not path.is_file() or path.is_symlink():
        raise PreflightError("coordinator config must be a regular file")
    if metadata.st_mode & 0o077:
        raise PreflightError("coordinator config mode must be 0600")


def _service_user(name: str) -> pwd.struct_passwd:
    try:
        return pwd.getpwnam(name)
    except KeyError as exc:
        raise PreflightError("service user does not exist") from exc


def _readable_by_user(path: Path, user: pwd.struct_passwd) -> bool:
    if os.geteuid() == user.pw_uid:
        return os.access(path, os.R_OK)
    runuser = shutil.which("runuser") or shutil.which("sudo")
    if not runuser:
        return False
    if runuser.endswith("runuser"):
        command = [runuser, "-u", user.pw_name, "--", "/usr/bin/test", "-r", str(path)]
    else:
        command = [runuser, "-n", "-u", user.pw_name, "--", "/usr/bin/test", "-r", str(path)]
    return subprocess.run(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=10,
    ).returncode == 0


def _check_free_space(path: Path, minimum: int) -> None:
    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        raise PreflightError("filesystem space check failed") from exc
    if free < minimum:
        raise PreflightError("free space is below the activation minimum")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight PixelBox activation")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--service-user", required=True)
    parser.add_argument("--expected-sha")
    parser.add_argument("--free-min-bytes", type=int, default=DEFAULT_MIN_FREE)
    args = parser.parse_args(argv)
    try:
        user = _service_user(args.service_user)
        _private_file(args.config)
        try:
            metadata = args.config.stat()
        except OSError as exc:
            raise PreflightError("coordinator config is unavailable") from exc
        if metadata.st_uid != user.pw_uid:
            raise PreflightError("coordinator config owner is not the service user")
        if metadata.st_gid != user.pw_gid:
            raise PreflightError("coordinator config group is not the service user's primary group")
        if not _readable_by_user(args.config, user):
            raise PreflightError("coordinator config is not readable by the service user")
        try:
            root_metadata = args.config_root.stat()
        except OSError as exc:
            raise PreflightError("configuration root is unavailable") from exc
        if not args.config_root.is_dir() or args.config_root.is_symlink():
            raise PreflightError("configuration root is not a directory")
        if root_metadata.st_uid != user.pw_uid:
            raise PreflightError("configuration root owner is not the service user")
        if root_metadata.st_mode & 0o007:
            raise PreflightError("configuration root must not be world-accessible")
        release = args.release_root
        if not release.is_dir() or release.is_symlink():
            raise PreflightError("release root is unavailable")
        for relative in REQUIRED_RELEASE_FILES:
            if not (release / relative).is_file():
                raise PreflightError("release is missing a required file")
        if args.expected_sha:
            marker = (release / ".orchestra-release-sha").read_text().strip()
            if marker != args.expected_sha:
                raise PreflightError("release marker does not match the requested SHA")
        _check_free_space(release, args.free_min_bytes)
        _check_free_space(args.config_root, args.free_min_bytes)
        # Import from the staged release, not from the caller's checkout.
        sys.path.insert(0, str(release))
        from omcp.controller import Config  # type: ignore
        Config.load(args.config)
    except (PreflightError, OSError, ValueError) as exc:
        print(f"PREFLIGHT FAILED: {exc}", file=sys.stderr)
        return 1
    print("PREFLIGHT OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
