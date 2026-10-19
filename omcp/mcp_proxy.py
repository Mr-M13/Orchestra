"""Stdio-to-Unix-socket proxy for the persistent OMCP coordinator.

The authenticated tunnel starts this tiny process as its stdio MCP command.  It
does not own task state or policy; it only relays newline-delimited JSON-RPC to
the coordinator's owner-only socket.
"""

from __future__ import annotations

import argparse
import os
import selectors
import socket
import sys
import time
from pathlib import Path


def connect(path: Path, timeout: float = 30.0) -> socket.socket:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
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
