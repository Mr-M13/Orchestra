"""Stdio-to-Unix-socket proxy for the persistent OMCP coordinator.

The authenticated tunnel starts this tiny process as its stdio MCP command.  It
does not own task state or policy; it only relays newline-delimited JSON-RPC to
the coordinator's owner-only socket.
"""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import selectors
import socket
import stat
import sys
import time
from pathlib import Path


def _validate_socket_path(path: Path, expected_group: str | None = None) -> None:
    """Fail closed unless this user owns an owner/group-only coordinator socket.

    The tunnel proxy runs as the socket owner.  The coordinator may also expose
    the same socket to the dedicated HTTP gateway group.  A 0660 socket is
    accepted only when its group is the configured gateway group and its parent
    directory is not group/other writable; 0600 remains the default.
    """
    metadata = path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode):
        raise ConnectionError("OMCP coordinator endpoint is not a Unix socket")
    if metadata.st_uid != os.getuid():
        raise PermissionError("OMCP coordinator socket must be owned by this user")
    mode = metadata.st_mode & 0o777
    if mode == 0o600:
        return
    expected_group = expected_group or os.environ.get("ORCHESTRA_OMCP_SOCKET_GROUP")
    if mode != 0o660 or not expected_group:
        raise PermissionError("OMCP coordinator socket must be owner/group-only")
    try:
        expected_gid = grp.getgrnam(expected_group).gr_gid
    except KeyError as exc:
        raise PermissionError("configured coordinator socket group is unavailable") from exc
    if metadata.st_gid != expected_gid:
        raise PermissionError("coordinator socket group does not match policy")
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022:
        raise PermissionError("coordinator runtime directory is not private")


def validate_gateway_socket(path: Path, owner: str, group: str) -> None:
    """Validate the one group-shared socket form used by the HTTP gateway.

    The coordinator runs as the owner and creates a 0660 socket in its private
    runtime directory.  The gateway runs as a different, group-only identity.
    Accepting the socket must therefore verify the exact owner, exact group,
    exact group-only mode, and a non-writable parent directory instead of
    loosening the stricter owner-only check used by the stdio tunnel proxy.
    """
    if not owner or not group:
        raise ValueError("gateway socket owner and group are required")
    try:
        expected_uid = pwd.getpwnam(owner).pw_uid
        expected_gid = grp.getgrnam(group).gr_gid
    except KeyError as exc:
        raise PermissionError("gateway socket owner or group is unavailable") from exc
    metadata = path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode):
        raise ConnectionError("OMCP coordinator endpoint is not a Unix socket")
    if (metadata.st_uid != expected_uid
            or metadata.st_gid != expected_gid
            or metadata.st_mode & 0o777 != 0o660):
        raise PermissionError("OMCP coordinator socket must be owner/group-only")
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != expected_uid
            or parent.st_mode & 0o022):
        raise PermissionError("OMCP coordinator runtime directory is not private")


def connect_gateway(
    path: Path, owner: str, group: str, timeout: float = 10.0,
) -> socket.socket:
    """Connect to the coordinator using the gateway's explicit identity policy."""
    validate_gateway_socket(path, owner, group)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(path))
    except OSError:
        connection.close()
        raise
    return connection


def connect(path: Path, timeout: float = 30.0) -> socket.socket:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            _validate_socket_path(path)
        except FileNotFoundError as exc:
            last_error = exc
            time.sleep(0.25)
            continue
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.connect(str(path))
            return connection
        except OSError as exc:
            last_error = exc
            connection.close()
            time.sleep(0.25)
    raise ConnectionError(
        f"OMCP coordinator socket is unavailable ({type(last_error).__name__})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Proxy stdio MCP to the OMCP coordinator")
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args(argv)
    connection = connect(args.socket.expanduser().resolve())
    connection.setblocking(True)
    selector = selectors.DefaultSelector()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    selector.register(stdin, selectors.EVENT_READ, "stdin")
    selector.register(connection, selectors.EVENT_READ, "coordinator")
    stdin_open = True
    try:
        while True:
            for key, _ in selector.select(timeout=1.0):
                if key.data == "stdin":
                    data = os.read(stdin.fileno(), 65_536)
                    if not data:
                        stdin_open = False
                        selector.unregister(stdin)
                        try:
                            connection.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        continue
                    try:
                        connection.sendall(data)
                    except (BrokenPipeError, OSError):
                        return 1
                else:
                    data = connection.recv(65_536)
                    if not data:
                        return 0
                    stdout.write(data)
                    stdout.flush()
            if not stdin_open and not selector.get_map():
                return 0
    finally:
        selector.close()
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
