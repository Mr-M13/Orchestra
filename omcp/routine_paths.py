"""Safe materialisation of routine-development writable paths.

Writable paths are always relative to one isolated task worktree.  The
coordinator may create missing directories inside that worktree so a permitted
new development area exists before the worker starts.  It never grants access
outside the worktree and rejects symlinks, traversal, Git metadata and
non-directory targets.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path


_SAFE_PART = re.compile(r"[A-Za-z0-9_-]+")


class UnsafeWritablePath(ValueError):
    """A writable path is not safe to materialise inside a task worktree."""


def normalize_writable_path(value: str) -> str:
    """Validate and normalise one owner-configured relative writable path."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise UnsafeWritablePath("writable path must be a non-empty relative path")
    candidate = Path(value)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or value in {".", ".git", ".codex"}
        or any(part in {".git", ".codex"} for part in candidate.parts)
        or not all(_SAFE_PART.fullmatch(part) for part in candidate.parts)
    ):
        raise UnsafeWritablePath("writable path must be a safe relative path")
    return candidate.as_posix().rstrip("/")


def materialize_writable_paths(
    workspace: Path, writable_paths: list[str] | tuple[str, ...],
) -> tuple[Path, ...]:
    """Create missing authorised directories inside an isolated worktree.

    Existing directories are accepted unchanged.  Missing directories are
    created with owner-only permissions.  Any symlink component, escaping
    path, non-directory component, Git metadata path or unsafe relative path
    fails closed.  The caller must pass the task's isolated worktree.
    """
    workspace = Path(workspace)
    if workspace.is_symlink() or not workspace.is_dir():
        raise UnsafeWritablePath("task workspace must be a real directory")
    root = workspace.resolve(strict=True)
    materialized: list[Path] = []
    seen: set[str] = set()

    for value in writable_paths:
        relative = normalize_writable_path(value)
        if relative in seen:
            raise UnsafeWritablePath("duplicate writable path")
        seen.add(relative)
        current = root
        for part in Path(relative).parts:
            target = current / part
            try:
                metadata = target.lstat()
            except FileNotFoundError:
                try:
                    target.mkdir(mode=0o700)
                except OSError as exc:
                    raise UnsafeWritablePath(
                        "authorised writable path could not be materialised"
                    ) from exc
                metadata = target.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise UnsafeWritablePath("writable path may not traverse a symlink")
            if not stat.S_ISDIR(metadata.st_mode):
                raise UnsafeWritablePath("writable path must be a directory")
            current = target

        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise UnsafeWritablePath("writable path escapes the task worktree")
        relative_parts = resolved.relative_to(root).parts
        if any(part in {".git", ".codex"} for part in relative_parts):
            raise UnsafeWritablePath("writable path may not target Git metadata")
        materialized.append(resolved)
    return tuple(materialized)
