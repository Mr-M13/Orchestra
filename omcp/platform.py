"""Host portability helpers for the Orchestra coordinator.

The coordinator originated on macOS, where Seatbelt provided the worker
sandbox.  PixelBox is Linux/ARM64, so command construction, sandbox backends and
supported executable locations must be selected at runtime rather than baked
into the task controller.

Nothing in this module trusts task input.  It only translates an already
validated, owner-configured execution policy into an operating-system command.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


class SandboxUnavailable(RuntimeError):
    """Raised when the host cannot provide the requested isolation boundary."""


def host_platform() -> str:
    """Return a small, stable platform name used in diagnostics and audit."""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def is_linux_arm64() -> bool:
    return host_platform() == "linux" and os.uname().machine.lower() in {
        "aarch64", "arm64",
    }


def canonical_git_executable() -> str:
    """Resolve Git without assuming a macOS installation path."""
    discovered = shutil.which("git")
    if not discovered:
        raise RuntimeError("Git is not available on the service PATH")
    return str(Path(discovered).resolve(strict=True))


def default_opencode_executable() -> Path:
    """Return the reviewed default OpenCode CLI location for this host.

    Operators may override the location with ``OMCP_OPENCODE_CLI``.  The value
    is still resolved and compared by ``canonical_opencode_executable`` before a
    worker can use it; the environment variable is owner/operator controlled and
    is never accepted from an MCP request.
    """
    override = os.environ.get("OMCP_OPENCODE_CLI")
    if override:
        return Path(override).expanduser()
    if host_platform() == "darwin":
        return Path(
            "/Users/hamed/Library/Application Support/ai.opencode.desktop/"
            "cli/2.0.32-arm64/opencode-cli"
        )
    return Path("/opt/orchestra/bin/opencode-cli")


def sandbox_backend(requested: str = "auto") -> str:
    """Resolve the isolation backend, failing closed for unknown requests."""
    if requested not in {"auto", "seatbelt", "bubblewrap"}:
        raise ValueError("sandbox backend must be auto, seatbelt, or bubblewrap")
    platform = host_platform()
    if requested == "seatbelt":
        return "seatbelt" if platform == "darwin" else _unavailable(requested, platform)
    if requested == "bubblewrap":
        return "bubblewrap" if platform == "linux" else _unavailable(requested, platform)
    if platform == "darwin":
        return "seatbelt"
    if platform == "linux":
        return "bubblewrap"
    return _unavailable("auto", platform)


def _unavailable(requested: str, platform: str) -> str:
    raise SandboxUnavailable(
        f"sandbox backend {requested!r} is not available on {platform}"
    )


def sandbox_executable(backend: str) -> str:
    if backend == "seatbelt":
        candidate = Path("/usr/bin/sandbox-exec")
        if not candidate.is_file():
            raise SandboxUnavailable("macOS Seatbelt is unavailable")
        return str(candidate)
    if backend == "bubblewrap":
        discovered = shutil.which("bwrap")
        if not discovered:
            raise SandboxUnavailable(
                "bubblewrap (bwrap) is required for the Linux worker sandbox"
            )
        return str(Path(discovered).resolve(strict=True))
    raise ValueError("unknown sandbox backend")


def _seatbelt_quote(value: Path | str) -> str:
    text = str(value)
    if any(ord(character) < 0x20 or ord(character) == 0x7f for character in text):
        raise ValueError("Seatbelt values may not contain control characters")
    return text.replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class SandboxPolicy:
    """A validated filesystem and network policy for one worker process.

    ``read_paths`` and ``write_paths`` are directories.  ``read_files`` are
    individual files such as a prompt or credential.  Provider hosts are
    HTTPS-only.  Loopback rules are exact ports and are currently supported
    only by Seatbelt; Linux must fail closed until an equivalent filtered
    local-network boundary is implemented.
    """

    read_paths: tuple[Path, ...] = ()
    read_files: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()
    network_hosts: tuple[str, ...] = ()
    loopback_bind_ports: tuple[int, ...] = ()
    loopback_connect_ports: tuple[int, ...] = ()
    backend: str = "auto"
    workdir: Path | None = None
    environment: dict[str, str] = field(default_factory=dict)

    def normalized(self) -> "SandboxPolicy":
        environment = dict(self.environment)
        if not all(
            isinstance(key, str) and key and "=" not in key and "\x00" not in key
            and isinstance(value, str) and "\x00" not in value
            for key, value in environment.items()
        ):
            raise ValueError("sandbox environment must contain valid strings")
        if not all(
            isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65_535
            for port in (*self.loopback_bind_ports, *self.loopback_connect_ports)
        ):
            raise ValueError("sandbox loopback ports must be integers from 1 to 65535")
        if not all(
            isinstance(host, str) and re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host,
            ) and "*" not in host and ".." not in host
            for host in self.network_hosts
        ):
            raise ValueError("sandbox network hosts must be fixed DNS names or addresses")
        return SandboxPolicy(
            read_paths=tuple(Path(value).resolve() for value in self.read_paths),
            read_files=tuple(Path(value).resolve() for value in self.read_files),
            write_paths=tuple(Path(value).resolve() for value in self.write_paths),
            network_hosts=tuple(self.network_hosts),
            loopback_bind_ports=tuple(self.loopback_bind_ports),
            loopback_connect_ports=tuple(self.loopback_connect_ports),
            backend=self.backend,
            workdir=Path(self.workdir).resolve() if self.workdir else None,
            environment=environment,
        )


def _seatbelt_profile(policy: SandboxPolicy, executable: str) -> str:
    rules = ["(version 1)", "(deny default)", '(import "system.sb")']
    rules.append(
        f'(allow process-exec (literal "{_seatbelt_quote(executable)}"))'
    )
    rules.extend(f'(allow file-read* (subpath "{_seatbelt_quote(path)}"))'
                 for path in policy.read_paths)
    rules.extend(f'(allow file-read* (literal "{_seatbelt_quote(path)}"))'
                 for path in policy.read_files)
    rules.extend(f'(allow file-write* (subpath "{_seatbelt_quote(path)}"))'
                 for path in policy.write_paths)
    if policy.network_hosts:
        rules.extend(
            f'(allow network-outbound (remote tcp "{_seatbelt_quote(host)}:443"))'
            for host in policy.network_hosts
        )
    for port in policy.loopback_bind_ports:
        rules.extend((
            f'(allow network-bind (local tcp "localhost:{port}"))',
            f'(allow network-inbound (local tcp "localhost:{port}"))',
        ))
    rules.extend(
        f'(allow network-outbound (remote tcp "localhost:{port}"))'
        for port in policy.loopback_connect_ports
    )
    return "\n".join(rules)


def _bubblewrap_command(policy: SandboxPolicy, command: Sequence[str]) -> list[str]:
    if policy.loopback_bind_ports or policy.loopback_connect_ports:
        raise SandboxUnavailable(
            "exact loopback-port filtering is not available with bubblewrap"
        )
    bwrap = sandbox_executable("bubblewrap")
    args = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        # Isolate every namespace first. A provider adapter may deliberately
        # re-share only the network namespace below.
        "--unshare-all",
        # Do not leak the coordinator/service environment into a worker.
        "--clearenv",
        # A private, disposable view of the host.  Only explicit paths below are
        # visible to the worker.
        "--tmpfs", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--dir", "/run",
        "--dir", "/etc",
        # Dynamic runtime and TLS trust are read-only inputs, not user data.
        "--ro-bind-try", "/usr", "/usr",
        "--ro-bind-try", "/bin", "/bin",
        "--ro-bind-try", "/sbin", "/sbin",
        "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--ro-bind-try", "/etc/ssl", "/etc/ssl",
        "--ro-bind-try", "/etc/ca-certificates", "/etc/ca-certificates",
        "--ro-bind-try", "/etc/resolv.conf", "/etc/resolv.conf",
        "--ro-bind-try", "/etc/hosts", "/etc/hosts",
        "--ro-bind-try", "/etc/nsswitch.conf", "/etc/nsswitch.conf",
        "--ro-bind-try", "/etc/passwd", "/etc/passwd",
        "--ro-bind-try", "/etc/group", "/etc/group",
    ]
    child_environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp",
        **policy.environment,
    }
    for name, value in sorted(child_environment.items()):
        args.extend(["--setenv", name, value])
    write_paths = set(policy.write_paths)
    for path in policy.read_paths:
        if not path.exists():
            raise SandboxUnavailable(f"sandbox read path does not exist: {path}")
        if path not in write_paths:
            args.extend(["--ro-bind", str(path), str(path)])
    for path in policy.read_files:
        if not path.exists():
            raise SandboxUnavailable(f"sandbox read file does not exist: {path}")
        args.extend(["--ro-bind", str(path), str(path)])
    for path in policy.write_paths:
        if not path.exists():
            raise SandboxUnavailable(f"sandbox write path does not exist: {path}")
        args.extend(["--bind", str(path), str(path)])

    # bubblewrap cannot filter a shared network namespace by destination host.
    # Denying the namespace entirely is therefore the only safe default; the
    # direct HTTPS adapters explicitly request their single provider host and
    # are documented as depending on host egress policy for optional filtering.
    if policy.network_hosts:
        args.append("--share-net")
    else:
        args.append("--unshare-net")
    if policy.workdir is not None:
        args.extend(["--chdir", str(policy.workdir)])
    args.append("--")
    args.extend(command)
    return args


def sandbox_command(policy: SandboxPolicy, command: Sequence[str]) -> list[str]:
    """Wrap ``command`` in the host's supported sandbox.

    The returned argument list is always executed without a shell.
    """
    if not command or not all(isinstance(part, str) and part for part in command):
        raise ValueError("sandbox command must be a non-empty argument list")
    normalized = policy.normalized()
    backend = sandbox_backend(normalized.backend)
    if backend == "seatbelt":
        executable = Path(command[0])
        if not executable.is_absolute():
            raise ValueError("Seatbelt executable must be an absolute path")
        profile = _seatbelt_profile(normalized, str(executable))
        return [sandbox_executable("seatbelt"), "-p", profile, *command]
    return _bubblewrap_command(normalized, command)


def describe_sandbox(requested: str = "auto") -> dict[str, object]:
    """Return safe diagnostic information without probing worker credentials."""
    try:
        backend = sandbox_backend(requested)
        executable = sandbox_executable(backend)
        available = True
        error = None
    except (SandboxUnavailable, ValueError) as exc:
        backend = requested
        executable = None
        available = False
        error = type(exc).__name__
    return {
        "platform": host_platform(),
        "architecture": os.uname().machine,
        "backend": backend,
        "available": available,
        "executable": executable,
        "error": error,
    }


def ensure_owner_only(path: Path) -> None:
    """Raise unless ``path`` is an owner-only regular file."""
    stat = path.stat()
    if not path.is_file() or stat.st_mode & 0o077 or stat.st_uid != os.getuid():
        raise PermissionError(f"{path.name} must be an owner-only regular file")


def allowed_relative_paths(values: Iterable[str]) -> bool:
    return all(
        isinstance(value, str) and value and ".." not in Path(value).parts
        and not Path(value).is_absolute()
        for value in values
    )
