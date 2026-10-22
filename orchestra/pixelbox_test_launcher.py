"""Trusted preparation/receipt helpers for the socket-free PixelBox canary."""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import re
import resource
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PROFILE = "pixelbox-socket-free-canary-v1"
MANIFEST_RELATIVE = Path("deploy/pixelbox/test-manifest.json")
OUTPUT_LIMIT = 32_768
FIXED_COMMAND = ("/usr/bin/python3", "-m", "unittest", "tests.test_socket_free_canary", "-q")
FIXED_TESTS = ("tests/test_socket_free_canary.py",)


class LaunchRefused(ValueError):
    pass


@dataclass(frozen=True)
class Release:
    sha: str; path: Path; digest: str; manifest_digest: str; test_tree_digest: str


@dataclass(frozen=True)
class Job:
    version: int; profile: str; sha: str; release_digest: str; manifest_digest: str
    test_tree_digest: str; command: tuple[str, ...]; timeout_seconds: int; output_limit: int
    boot_id: str; prepared_monotonic_ms: int


@dataclass(frozen=True)
class UnitOutcome:
    invocation_id: str; active_state: str; result: str; exec_main_status: int
    started_monotonic_ms: int; exited_monotonic_ms: int


@dataclass(frozen=True)
class Receipt:
    version: int; profile: str; sha: str; release_digest: str; manifest_digest: str
    test_tree_digest: str; invocation_id: str; unit_result: str; exec_main_status: int
    started_monotonic_ms: int; exited_monotonic_ms: int
    output_sha256: str; output_bytes: int


def _protected(info: os.stat_result) -> bool:
    return info.st_uid == 0 and not info.st_mode & 0o022


def _require_root() -> None:
    if os.geteuid() != 0:
        raise LaunchRefused("trusted preparation and receipt operations must run as root")


def _regular(path: Path, maximum: int = 1_048_576) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not _protected(info):
        raise LaunchRefused(f"unsafe file: {path}")
    if info.st_size > maximum:
        raise LaunchRefused(f"oversized file: {path}")
    return path.read_bytes()


def _protected_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or not _protected(info):
        raise LaunchRefused(f"unsafe directory: {path}")


def _release_digest(root: Path, protected: bool = True) -> str:
    digest = hashlib.sha256()
    excluded = {".orchestra-release-sha", ".orchestra-release-digest"}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            if protected:
                _protected_directory(path)
            continue
        if protected and (not stat.S_ISREG(info.st_mode) or not _protected(info)):
            raise LaunchRefused(f"unsafe release entry: {relative}")
        digest.update(relative.encode() + b"\0")
        if stat.S_ISREG(info.st_mode):
            digest.update(b"X\0" if info.st_mode & 0o111 else b"F\0")
            digest.update(path.read_bytes() + b"\0")
        else:
            raise LaunchRefused(f"unsupported release entry: {relative}")
    return digest.hexdigest()


def _fixed_manifest(root: Path) -> str:
    raw = _regular(root / MANIFEST_RELATIVE, 4096)
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LaunchRefused("invalid fixed test manifest") from error
    expected: dict[str, Any] = {"version": 1, "profile": PROFILE,
        "command": list(FIXED_COMMAND), "tests": list(FIXED_TESTS),
        "timeout_seconds": 120, "output_limit": OUTPUT_LIMIT}
    if manifest != expected:
        raise LaunchRefused("test manifest is not the approved socket-free profile")
    return hashlib.sha256(raw).hexdigest()


def _test_tree_digest(root: Path, protected: bool = True) -> str:
    if protected:
        _protected_directory(root / "tests")
    digest = hashlib.sha256()
    for relative in FIXED_TESTS:
        path = root / relative
        raw = _regular(path) if protected else path.read_bytes()
        digest.update(relative.encode() + b"\0" + hashlib.sha256(raw).digest())
    return digest.hexdigest()


def verified_release(releases_root: Path, sha: str) -> Release:
    if not SHA_RE.fullmatch(sha):
        raise LaunchRefused("SHA must be exactly 40 lowercase hexadecimal characters")
    _protected_directory(releases_root)
    root = releases_root / sha
    _protected_directory(root)
    marker = _regular(root / ".orchestra-release-sha", 41).decode().strip()
    recorded = _regular(root / ".orchestra-release-digest", 65).decode().strip()
    if marker != sha or not re.fullmatch(r"[0-9a-f]{64}", recorded):
        raise LaunchRefused("release marker or digest does not match requested SHA")
    if _release_digest(root) != recorded:
        raise LaunchRefused("staged release digest does not match its tree")
    return Release(sha, root, recorded, _fixed_manifest(root), _test_tree_digest(root))


def _atomic_root_json(directory: Path, filename: str, value: object,
                      mode: int = 0o600, group: int | None = None) -> Path:
    _protected_directory(directory)
    target, temporary = directory / filename, directory / (filename + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(asdict(value), output, sort_keys=True, separators=(",", ":"))
            output.flush(); os.fsync(output.fileno())
        if group is not None:
            os.chown(temporary, -1, group)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f-]{36}", value):
        raise LaunchRefused("invalid host boot identity")
    return value


def prepare_job(releases_root: Path, sha: str, jobs_dir: Path,
                worker_group: str = "orchestra-test") -> Path:
    _require_root()
    release = verified_release(releases_root, sha)
    try:
        worker_gid = grp.getgrnam(worker_group).gr_gid
    except KeyError as error:
        raise LaunchRefused("test worker group does not exist") from error
    return _atomic_root_json(jobs_dir, f"{sha}.json", Job(
        1, PROFILE, release.sha, release.digest, release.manifest_digest,
        release.test_tree_digest, FIXED_COMMAND, 120, OUTPUT_LIMIT, _boot_id(),
        time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000), mode=0o640, group=worker_gid)


def _load_job(path: Path) -> Job:
    raw = _regular(path, 8192)
    try:
        value = json.loads(raw); value["command"] = tuple(value["command"]); job = Job(**value)
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise LaunchRefused("invalid sealed test job") from error
    if (job.version, job.profile, job.command, job.timeout_seconds, job.output_limit) != \
            (1, PROFILE, FIXED_COMMAND, 120, OUTPUT_LIMIT) or not SHA_RE.fullmatch(job.sha) \
            or not re.fullmatch(r"[0-9a-f-]{36}", job.boot_id) or job.prepared_monotonic_ms < 0:
        raise LaunchRefused("sealed job does not name the approved profile")
    return job


def run_job(job_path: Path, releases_root: Path) -> int:
    if os.geteuid() == 0:
        raise LaunchRefused("test runner must not run as root")
    job = _load_job(job_path)
    root = releases_root / job.sha
    if root.is_symlink() or not (root / "tests").is_dir():
        raise LaunchRefused("sealed release disappeared before execution")
    manifest = hashlib.sha256((root / MANIFEST_RELATIVE).read_bytes()).hexdigest()
    marker = (root / ".orchestra-release-sha").read_text(encoding="ascii").strip()
    digest = (root / ".orchestra-release-digest").read_text(encoding="ascii").strip()
    if marker != job.sha or digest != job.release_digest or _release_digest(root, protected=False) != digest \
            or manifest != job.manifest_digest or _test_tree_digest(root, protected=False) != job.test_tree_digest:
        raise LaunchRefused("executable test tree differs from sealed job")

    def limit_output() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (job.output_limit, job.output_limit))

    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(job.command, cwd=root, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True, preexec_fn=limit_output,
            env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            status = process.wait(timeout=job.timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            status = 124
        output.seek(0)
        payload = output.read(job.output_limit + 1)
    if len(payload) > job.output_limit:
        os.write(1, b"test output exceeded fixed bound\n")
        return 75
    os.write(1, payload)
    return status


def _unit_outcome(sha: str) -> UnitOutcome:
    completed = subprocess.run(
        ["/bin/systemctl", "show", "--property=InvocationID", "--property=ActiveState",
         "--property=Result", "--property=ExecMainStatus",
         "--property=ExecMainStartTimestampMonotonic", "--property=ExecMainExitTimestampMonotonic",
         "--value", f"orchestra-test@{sha}.service"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False, timeout=15,
    )
    values = completed.stdout.splitlines()
    if completed.returncode or len(values) != 6:
        raise LaunchRefused("could not obtain terminal state from systemd")
    try:
        outcome = UnitOutcome(values[0], values[1], values[2], int(values[3]),
                              int(values[4]) // 1_000, int(values[5]) // 1_000)
    except ValueError as error:
        raise LaunchRefused("systemd returned an invalid execution status") from error
    if not re.fullmatch(r"[0-9a-f]{32}", outcome.invocation_id) or outcome.active_state == "active":
        raise LaunchRefused("systemd unit is stale or still active")
    return outcome


def _invocation_output(invocation_id: str) -> bytes:
    completed = subprocess.run(
        ["/bin/journalctl", "--quiet", "--no-pager", "--output=cat",
         f"_SYSTEMD_INVOCATION_ID={invocation_id}"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=15,
    )
    if completed.returncode or len(completed.stdout) > OUTPUT_LIMIT:
        raise LaunchRefused("invocation output is unavailable or exceeds its fixed bound")
    return completed.stdout


def record_receipt(releases_root: Path, job_path: Path, receipts_dir: Path) -> Path:
    _require_root(); job = _load_job(job_path); release = verified_release(releases_root, job.sha)
    if (release.digest, release.manifest_digest, release.test_tree_digest) != \
            (job.release_digest, job.manifest_digest, job.test_tree_digest):
        raise LaunchRefused("release differs from the sealed test job")
    outcome = _unit_outcome(job.sha)
    if outcome.active_state == "active" or job.boot_id != _boot_id() or outcome.started_monotonic_ms < job.prepared_monotonic_ms \
            or outcome.exited_monotonic_ms < outcome.started_monotonic_ms:
        raise LaunchRefused("unit invocation is stale or predates the sealed job")
    output = _invocation_output(outcome.invocation_id)
    if outcome.result != "success" or outcome.exec_main_status != 0:
        raise LaunchRefused("unit did not complete the sealed test job successfully")
    return _atomic_root_json(receipts_dir, f"{job.sha}.json", Receipt(
        1, PROFILE, job.sha, job.release_digest, job.manifest_digest, job.test_tree_digest,
        outcome.invocation_id, outcome.result, outcome.exec_main_status,
        outcome.started_monotonic_ms, outcome.exited_monotonic_ms,
        hashlib.sha256(output).hexdigest(), len(output)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest="operation", required=True)
    prepare = sub.add_parser("prepare"); prepare.add_argument("--releases-root", type=Path, required=True); prepare.add_argument("--sha", required=True); prepare.add_argument("--jobs-dir", type=Path, required=True)
    run = sub.add_parser("run"); run.add_argument("--job", type=Path, required=True); run.add_argument("--releases-root", type=Path, required=True)
    receipt = sub.add_parser("receipt"); receipt.add_argument("--releases-root", type=Path, required=True); receipt.add_argument("--job", type=Path, required=True); receipt.add_argument("--receipts-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare": prepare_job(args.releases_root, args.sha, args.jobs_dir); return 0
        if args.operation == "run": return run_job(args.job, args.releases_root)
        record_receipt(args.releases_root, args.job, args.receipts_dir); return 0
    except LaunchRefused as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
