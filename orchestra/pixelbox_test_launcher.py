"""Fixed-profile, non-authoritative test execution for a staged PixelBox release.

This module is deliberately not a task worker. The only executable profile is
the repository's unit-test suite and its only input is the SHA-named,
root-owned release directory created by ``deploy/pixelbox/install.sh``. A
result produced by this code is test evidence, never an approval, merge, or
deployment capability.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RESULT_LIMIT = 32_768
PROFILE = "python-unittest-v1"
FIXED_COMMAND = ("/usr/bin/python3", "-m", "unittest", "discover", "-s", "tests", "-q")


class LaunchRefused(ValueError):
    """Raised before a test process is allowed to start."""


@dataclass(frozen=True)
class Release:
    sha: str
    path: Path
    digest: str


@dataclass(frozen=True)
class TestEvidence:
    version: int
    profile: str
    sha: str
    release_digest: str
    outcome: str
    exit_code: int
    duration_ms: int
    output_sha256: str
    output: str


def _protected(info: os.stat_result) -> bool:
    return info.st_uid == 0 and not info.st_mode & 0o022


def _read_regular(path: Path, maximum: int = 256) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not _protected(info):
        raise LaunchRefused(f"unsafe release metadata: {path.name}")
    value = path.read_text(encoding="ascii").strip()
    if len(value) > maximum:
        raise LaunchRefused(f"oversized release metadata: {path.name}")
    return value


def verified_release(releases_root: Path, sha: str) -> Release:
    """Return only a direct, SHA-named, protected staged release."""
    if not SHA_RE.fullmatch(sha):
        raise LaunchRefused("SHA must be exactly 40 lowercase hexadecimal characters")
    root_info = releases_root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or not _protected(root_info):
        raise LaunchRefused("unsafe releases root")
    path = releases_root / sha
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or not _protected(info):
        raise LaunchRefused("release is not a protected directory")
    marker = _read_regular(path / ".orchestra-release-sha", 41)
    digest = _read_regular(path / ".orchestra-release-digest", 65)
    if marker != sha or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise LaunchRefused("release marker or digest does not match the requested SHA")
    tests = path / "tests"
    if not tests.is_dir() or tests.is_symlink():
        raise LaunchRefused("release does not contain a direct tests directory")
    return Release(sha=sha, path=path, digest=digest)


def fixed_command(release: Release) -> tuple[str, ...]:
    """Return the sole allowed command; there are no task-provided arguments."""
    del release
    return FIXED_COMMAND


def _atomic_result(path: Path, evidence: TestEvidence) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(asdict(evidence), output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def run_fixed_profile(releases_root: Path, sha: str, result_path: Path, timeout_seconds: int) -> TestEvidence:
    """Run the fixed profile, bounded and in a separate process group.

    The systemd unit is the security boundary. This function supplies the
    deterministic command, timeout and SHA-bound evidence format used inside
    that unit; it intentionally has no network, credential or provider inputs.
    """
    if not 1 <= timeout_seconds <= 3_600:
        raise LaunchRefused("timeout must be between 1 and 3600 seconds")
    release = verified_release(releases_root, sha)
    started = time.monotonic()
    process = subprocess.Popen(
        fixed_command(release), cwd=release.path, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False,
        start_new_session=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        payload, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            payload, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            payload, _ = process.communicate()
        outcome, exit_code = "timed_out", 124
    else:
        outcome = "passed" if process.returncode == 0 else "failed"
        exit_code = process.returncode
    payload = payload[:RESULT_LIMIT]
    duration_ms = int((time.monotonic() - started) * 1000)
    evidence = TestEvidence(
        version=1, profile=PROFILE, sha=release.sha, release_digest=release.digest,
        outcome=outcome, exit_code=exit_code, duration_ms=duration_ms,
        output_sha256=hashlib.sha256(payload).hexdigest(),
        output=payload.decode("utf-8", errors="replace"),
    )
    _atomic_result(result_path, evidence)
    return evidence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--releases-root", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args(argv)
    try:
        evidence = run_fixed_profile(args.releases_root, args.sha, args.result, args.timeout_seconds)
    except LaunchRefused as error:
        parser.error(str(error))
    return 0 if evidence.outcome == "passed" else evidence.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
