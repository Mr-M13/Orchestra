"""Stable, kernel-derived process identity for disposable workers.

The adapter must not treat a PID, a display command or a mutable ``ps`` field as
proof of worker identity.  Linux exposes a stable start time in
``/proc/<pid>/stat``.  macOS exposes PID, process group, UID and a microsecond
start time through ``proc_pidinfo(PROC_PIDTBSDINFO)``.

Unsupported or unavailable identity sources fail closed: they return ``None``
and the adapter marks the outcome unknown.  No weak ``ps`` fallback is used for
authorisation, cancellation or recovery.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROC_PIDTBSDINFO = 3
DARWIN_STRUCT_SIZE = 136
_MAXCOMLEN = 16


class ProcessIdentityError(ValueError):
    """A durable process identity is malformed or unsafe."""


@dataclass(frozen=True)
class ProcessIdentity:
    """Exact identity of one live process on one platform.

    Linux uses ``start_ticks`` (the kernel starttime field).  Darwin uses
    ``start_seconds`` and ``start_microseconds`` from ``proc_pidinfo``.  Both
    require the PID, process group and effective UID to match as well.
    """

    platform: str
    pid: int
    pgid: int
    uid: int
    start_seconds: int = 0
    start_microseconds: int = 0
    start_ticks: int | None = None

    def __post_init__(self) -> None:
        if self.platform not in {"linux", "darwin"}:
            raise ProcessIdentityError("unsupported process identity platform")
        for field in ("pid", "pgid"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ProcessIdentityError(f"{field} must be a positive integer")
        if not isinstance(self.uid, int) or isinstance(self.uid, bool) or self.uid < 0:
            raise ProcessIdentityError("uid must be a non-negative integer")
        if (
            not isinstance(self.start_seconds, int)
            or isinstance(self.start_seconds, bool)
            or self.start_seconds < 0
            or not isinstance(self.start_microseconds, int)
            or isinstance(self.start_microseconds, bool)
            or not 0 <= self.start_microseconds < 1_000_000
        ):
            raise ProcessIdentityError("process start time is invalid")
        if self.platform == "linux":
            if (
                not isinstance(self.start_ticks, int)
                or isinstance(self.start_ticks, bool)
                or self.start_ticks < 0
            ):
                raise ProcessIdentityError("Linux identity requires start_ticks")
        elif self.start_ticks is not None:
            raise ProcessIdentityError("Darwin identity must not contain start_ticks")

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "pid": self.pid,
            "pgid": self.pgid,
            "uid": self.uid,
            "start_seconds": self.start_seconds,
            "start_microseconds": self.start_microseconds,
            "start_ticks": self.start_ticks,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ProcessIdentity":
        if not isinstance(payload, dict):
            raise ProcessIdentityError("process identity must be an object")
        allowed = {
            "platform", "pid", "pgid", "uid", "start_seconds",
            "start_microseconds", "start_ticks",
        }
        if set(payload) - allowed:
            raise ProcessIdentityError("process identity has unknown fields")
        try:
            return cls(**payload)
        except TypeError as error:
            raise ProcessIdentityError(str(error)) from None


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * _MAXCOMLEN),
        ("pbi_name", ctypes.c_char * (2 * _MAXCOMLEN)),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


_LIBPROC: Any = None
_LIBPROC_LOCK = threading.Lock()


def _darwin_libproc() -> Any:
    global _LIBPROC
    with _LIBPROC_LOCK:
        if _LIBPROC is None:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            libproc.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            libproc.proc_pidinfo.restype = ctypes.c_int
            _LIBPROC = libproc
        return _LIBPROC


def _linux_effective_uid(pid: int) -> int | None:
    try:
        lines = Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        if line.startswith("Uid:"):
            fields = line.split()
            if len(fields) < 3:
                return None
            try:
                return int(fields[2])
            except ValueError:
                return None
    return None


def _linux_identity(pid: int, expected_uid: int | None) -> ProcessIdentity | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 2:].split()
    if len(fields) < 20:
        return None
    try:
        pgid = int(fields[2])
        start_ticks = int(fields[19])
    except (ValueError, IndexError):
        return None
    if pgid != pid:
        return None
    uid = _linux_effective_uid(pid)
    if uid is None or (expected_uid is not None and uid != expected_uid):
        return None
    return ProcessIdentity(
        platform="linux",
        pid=pid,
        pgid=pgid,
        uid=uid,
        start_ticks=start_ticks,
    )


def _darwin_identity(pid: int, expected_uid: int | None) -> ProcessIdentity | None:
    if ctypes.sizeof(_ProcBSDInfo) != DARWIN_STRUCT_SIZE:
        return None
    try:
        libproc = _darwin_libproc()
    except OSError:
        return None
    info = _ProcBSDInfo()
    size = ctypes.sizeof(info)
    copied = libproc.proc_pidinfo(
        pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), size,
    )
    if copied != size:
        return None
    if info.pbi_pid != pid or info.pbi_pgid != pid:
        return None
    if expected_uid is not None and info.pbi_uid != expected_uid:
        return None
    return ProcessIdentity(
        platform="darwin",
        pid=pid,
        pgid=info.pbi_pgid,
        uid=info.pbi_uid,
        start_seconds=info.pbi_start_tvsec,
        start_microseconds=info.pbi_start_tvusec,
    )


def capture_process_identity(
    pid: int, *, expected_uid: int | None = None,
) -> ProcessIdentity | None:
    """Capture a stable identity, or return ``None`` if it cannot be proven."""

    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return None
    if expected_uid is not None and (
        not isinstance(expected_uid, int)
        or isinstance(expected_uid, bool)
        or expected_uid < 0
    ):
        return None
    if sys.platform.startswith("linux"):
        return _linux_identity(pid, expected_uid)
    if sys.platform == "darwin":
        return _darwin_identity(pid, expected_uid)
    return None
