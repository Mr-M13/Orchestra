"""Authoritative routine-development confinement policy.

Both the real Codex worker and the coordinator-controlled verification probe
consume the same policy object.  The probe must never reconstruct a stricter
sandbox independently; weakening this policy must weaken both paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROFILE_NAME = "orchestra-routine"


def _safe_read_path(value: str) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("read-only path must be absolute")
    if any(character.isspace() or character in {'"', "\n", "\r", "{", "}", "="}
           for character in value):
        raise ValueError("read-only path contains unsupported characters")
    return value


@dataclass(frozen=True)
class RoutineConfinementPolicy:
    """The single source of truth for routine worker confinement."""

    writable_paths: tuple[str, ...]
    read_only_paths: tuple[str, ...] = ()
    writable_files: tuple[str, ...] = ()
    network_enabled: bool = False
    deny_orchestra_config: bool = True
    deny_tmpdir: bool = True
    deny_slash_tmp: bool = True
    profile_name: str = PROFILE_NAME

    def __post_init__(self) -> None:
        from omcp.routine_paths import normalize_writable_file, normalize_writable_path

        normalized = tuple(normalize_writable_path(value) for value in self.writable_paths)
        normalized_files = tuple(normalize_writable_file(value) for value in self.writable_files)
        if set(normalized) & set(normalized_files):
            raise ValueError("a writable file cannot duplicate a writable directory")
        object.__setattr__(self, "writable_paths", normalized)
        object.__setattr__(self, "writable_files", normalized_files)
        object.__setattr__(
            self, "read_only_paths",
            tuple(_safe_read_path(value) for value in self.read_only_paths),
        )
        if self.profile_name != PROFILE_NAME:
            raise ValueError("routine confinement profile name is fixed")

    def config_args(self) -> list[str]:
        rules = ','.join([
            '"."="read"', '".orchestra-tmp"="write"',
            *(f'"{value}"="write"' for value in self.writable_paths),
            *(f'"{value}"="write"' for value in self.writable_files),
        ])
        args = [
            "-c", f'default_permissions="{self.profile_name}"',
            "-c", f'permissions.{self.profile_name}.filesystem.:root="deny"',
            "-c", f'permissions.{self.profile_name}.filesystem.:minimal="read"',
        ]
        if self.deny_orchestra_config:
            args.extend([
                "-c",
                f'permissions.{self.profile_name}.filesystem./etc/orchestra="deny"',
            ])
        for value in self.read_only_paths:
            args.extend([
                "-c",
                f'permissions.{self.profile_name}.filesystem.{value}="read"',
            ])
        if self.deny_tmpdir:
            args.extend([
                "-c", f'permissions.{self.profile_name}.filesystem.:tmpdir="deny"',
            ])
        if self.deny_slash_tmp:
            args.extend([
                "-c", f'permissions.{self.profile_name}.filesystem.:slash_tmp="deny"',
            ])
        args.extend([
            "-c",
            f'permissions.{self.profile_name}.filesystem.:workspace_roots={{{rules}}}',
            "-c",
            f'permissions.{self.profile_name}.network.enabled={str(self.network_enabled).lower()}',
        ])
        return args

    def worker_command(self, executable: str, workspace: Path, result_path: Path) -> list[str]:
        return [
            executable, "--ask-for-approval", "never", "exec", "--json",
            "--ignore-user-config", "--ignore-rules", "--ephemeral",
            *self.config_args(),
            "--cd", str(workspace),
            "--output-last-message", str(result_path), "-",
        ]

    def probe_command(self, executable: str, workspace: Path,
                      command: list[str]) -> list[str]:
        return [
            executable, "sandbox", *self.config_args(),
            "-P", self.profile_name, "-C", str(workspace), "--", *command,
        ]
