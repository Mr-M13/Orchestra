"""Safe materialisation of routine-development writable paths and files.

Writable grants are always relative to one isolated task worktree.  The
coordinator may create missing directories inside that worktree so a permitted
new development area exists before the worker starts.  File grants create only
their parent directories; the worker creates the authorised file itself.  This
module never grants access outside the worktree and rejects symlinks,
traversal, Git metadata and non-directory/non-regular targets.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path


_SAFE_PART = re.compile(r"[A-Za-z0-9_-]+")
_SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}")
_PROTECTED_PARTS = frozenset({".git", ".codex"})


class UnsafeWritablePath(ValueError):
    """A writable path or file is not safe to materialise in a worktree."""


def _validate_relative(value: str, *, file_path: bool) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise UnsafeWritablePath("writable path must be a non-empty relative path")
    if value.endswith("/") or "\\" in value:
        raise UnsafeWritablePath("writable path must be a safe relative path")
    candidate = Path(value)
    parts = candidate.parts
    if (
        candidate.is_absolute()
        or ".." in parts
        or not parts
        or any(part in _PROTECTED_PARTS for part in parts)
        or not all(_SAFE_PART.fullmatch(part) for part in parts[:-1])
        or not (hasattr(_SAFE_FILE_NAME, "fullmatch") and _SAFE_FILE_NAME.fullmatch(parts[-1]))
    ):
        raise UnsafeWritablePath("writable path must be a safe relative path")
    if file_path:
        if candidate.name in {"", ".", ".."} or not _SAFE_FILE_NAME.fullmatch(candidate.name):
            raise UnsafeWritablePath("writable file must name a regular relative file")
    elif not _SAFE_PART.fullmatch(parts[-1]):
        raise UnsafeWritablePath("writable directory must be a safe relative path")
    return candidate


def normalize_writable_path(value: str) -> str:
    """Validate and normalise one owner-configured writable directory."""
    return _validate_relative(value, file_path=False).as_posix().rstrip("/")


def normalize_writable_file(value: str) -> str:
    """Validate and normalise one owner-configured exact writable file."""
    return _validate_relative(value, file_path=True).as_posix()


def _ensure_directory(root: Path, current: Path, part: str) -> Path:
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
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise UnsafeWritablePath("writable path escapes the task worktree")
    return resolved


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
            current = _ensure_directory(root, current, part)
        materialized.append(current)
    return tuple(materialized)


def materialize_writable_files(
    workspace: Path, writable_files: list[str] | tuple[str, ...],
) -> tuple[Path, ...]:
    """Create safe parent directories for exact authorised worker files.

    The file itself is deliberately not created.  This keeps the completion
    contract and worker output inside the task's private state directory while
    allowing a worker to update one reviewed repository file without receiving
    a repository-wide write grant.
    """
    workspace = Path(workspace)
    if workspace.is_symlink() or not workspace.is_dir():
        raise UnsafeWritablePath("task workspace must be a real directory")
    root = workspace.resolve(strict=True)
    materialized: list[Path] = []
    seen: set[str] = set()

    for value in writable_files:
        relative = normalize_writable_file(value)
        if relative in seen:
            raise UnsafeWritablePath("duplicate writable file")
        seen.add(relative)
        parts = Path(relative).parts
        current = root
        for part in parts[:-1]:
            current = _ensure_directory(root, current, part)
        target = current / parts[-1]
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            # The parent is a real directory inside the worktree; do not create
            # the file itself.  The worker creates it through its sandbox.
            materialized.append(target)
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafeWritablePath("writable file may not be a symlink")
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeWritablePath("writable file target must be a regular file")
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise UnsafeWritablePath("writable file escapes the task worktree")
        relative_parts = resolved.relative_to(root).parts
        if any(part in _PROTECTED_PARTS for part in relative_parts):
            raise UnsafeWritablePath("writable file may not target Git metadata")
        materialized.append(resolved)
    return tuple(materialized)
