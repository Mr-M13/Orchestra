"""Peer identity extraction for Unix-domain authority sockets.

The authority never trusts a caller-supplied role or user id.  On Linux it uses
``SO_PEERCRED``; on macOS it uses ``LOCAL_PEERCRED``.  Unsupported platforms
fail closed instead of falling back to an unauthenticated protocol.
"""

from __future__ import annotations

import os
import socket
import struct
import sys
from dataclasses import dataclass


class PeerCredentialError(OSError):
    """The operating system did not provide a verifiable peer identity."""


@dataclass(frozen=True)
class PeerCredentials:
    uid: int
    gid: int
    pid: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.uid, int) or self.uid < 0:
            raise PeerCredentialError("peer uid is invalid")
        if not isinstance(self.gid, int) or self.gid < 0:
            raise PeerCredentialError("peer gid is invalid")
        if self.pid is not None and (
            not isinstance(self.pid, int) or self.pid < 1
        ):
            raise PeerCredentialError("peer pid is invalid")


def peer_credentials(sock: socket.socket) -> PeerCredentials:
    """Return kernel-derived credentials for a connected Unix socket.

    Linux exposes a ``struct ucred`` through ``SO_PEERCRED``.  Darwin exposes a
    ``struct xucred`` through ``LOCAL_PEERCRED``.  The Darwin variant has no
    peer pid, so ``pid`` is ``None`` there.
    """

    if not isinstance(sock, socket.socket):
        raise PeerCredentialError("peer_credentials requires a socket")

    if hasattr(socket, "SO_PEERCRED"):
        size = struct.calcsize("3i")
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
        if len(raw) < size:
            raise PeerCredentialError("SO_PEERCRED returned a short value")
        pid, uid, gid = struct.unpack("iII", raw)
        if pid < 1:
            raise PeerCredentialError("SO_PEERCRED returned an invalid pid")
        return PeerCredentials(uid=uid, gid=gid, pid=pid)

    if sys.platform == "darwin" and hasattr(socket, "LOCAL_PEERCRED"):
        # struct xucred: uint32 version, uid_t uid, short ngroups, gid_t groups[16]
        raw = sock.getsockopt(0, socket.LOCAL_PEERCRED, 128)
        if len(raw) < 16:
            raise PeerCredentialError("LOCAL_PEERCRED returned a short value")
        version, uid = struct.unpack_from("=II", raw, 0)
        if version != 0:
            raise PeerCredentialError("LOCAL_PEERCRED returned an unknown version")
        ngroups = struct.unpack_from("=H", raw, 8)[0]
        if ngroups < 1 or len(raw) < 12 + 4:
            raise PeerCredentialError("LOCAL_PEERCRED returned no group")
        gid = struct.unpack_from("=I", raw, 12)[0]
        return PeerCredentials(uid=uid, gid=gid, pid=None)

    raise PeerCredentialError(
        f"no supported Unix peer-credential mechanism on {sys.platform!r}"
    )


def require_current_uid(expected_uid: int) -> None:
    """Fail if the process is not running under the configured authority UID."""

    actual = os.geteuid()
    if actual != expected_uid:
        raise PeerCredentialError(
            f"authority must run as uid {expected_uid}, not {actual}"
        )
