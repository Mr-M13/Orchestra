"""Encrypted, restorable Orchestra and Nextcloud backups.

The production backup repository is deliberately not selected in code.  The
owner-only ``backup.json`` names the restic repository, password file and any
provider environment file.  A backup is reported healthy only after a recent
successful snapshot *and* a recent repository integrity check.  A timer or a
configured path alone is never evidence of a backup.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
STATUS_SCHEMA_VERSION = 1
DEFAULT_CONFIG = Path("/etc/orchestra/backup.json")
DEFAULT_STATUS = Path("/var/lib/orchestra-backup/status.json")
DEFAULT_STAGING = Path("/var/lib/orchestra-backup/staging")
DEFAULT_CACHE = Path("/var/lib/orchestra-backup/cache")
DEFAULT_REPOSITORY_PREFIXES = (
    "local:", "sftp:", "s3:", "b2:", "rest:", "rclone:",
)
MAX_ERROR_BYTES = 512
MAX_STATUS_BYTES = 64 * 1024


class BackupError(RuntimeError):
    """A conflict-free, bounded backup failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mode_is_private(path: Path) -> bool:
    try:
        metadata = path.stat()
    except OSError:
        return False
    return (
        path.is_file()
        and not path.is_symlink()
        and not metadata.st_mode & 0o077
        and os.access(path, os.R_OK)
    )


def _atomic_json(
    path: Path, value: dict[str, Any], *, mode: int = 0o600,
    directory_mode: int = 0o700,
) -> None:
    path.parent.mkdir(parents=True, mode=directory_mode, exist_ok=True)
    os.chmod(path.parent, directory_mode)
    temporary = path.with_name(path.name + ".tmp")
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(fd)
    temporary.replace(path)
    os.chmod(path, mode)


def _bounded_error(exc: BaseException) -> dict[str, str]:
    text = str(exc).replace("\n", " ").strip()
    return {
        "type": type(exc).__name__,
        "message": text.encode("utf-8")[:MAX_ERROR_BYTES].decode("utf-8", "replace"),
    }


def _read_owner_file(path: Path) -> dict[str, str]:
    if not _mode_is_private(path):
        raise BackupError("backup environment file must be private and owner-only")
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BackupError("backup environment file is unavailable") from exc
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or not key.replace("_", "").isalnum():
            raise BackupError("backup environment file contains an invalid entry")
        result[key] = value
    return result


def _require_private_file(path: Path, label: str) -> Path:
    if not _mode_is_private(path):
        raise BackupError(f"{label} must be a private owner-only regular file")
    return path


def _require_directory(path: Path, label: str, *, private: bool = False) -> Path:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise BackupError(f"{label} is unavailable") from exc
    if not path.is_dir() or path.is_symlink():
        raise BackupError(f"{label} must be a directory")
    if private and metadata.st_mode & 0o007:
        raise BackupError(f"{label} must not be world-accessible")
    return path


def _require_regular_file(path: Path, label: str) -> Path:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise BackupError(f"{label} is unavailable") from exc
    if not path.is_file() or path.is_symlink():
        raise BackupError(f"{label} must be a regular file")
    return path


def _ensure_private_directory(path: Path, label: str) -> Path:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise BackupError(f"{label} could not be created") from exc
    return _require_directory(path, label, private=True)


def _safe_relative_paths(values: object, label: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or not all(
            isinstance(value, str) and value and "\x00" not in value
            for value in values):
        raise BackupError(f"{label} must be a list of paths")
    return list(values)


@dataclass(frozen=True)
class Retention:
    daily: int = 7
    weekly: int = 4
    monthly: int = 6

    @classmethod
    def from_value(cls, value: object) -> "Retention":
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise BackupError("retention must be an object")
        parsed = cls(
            daily=value.get("daily", 7), weekly=value.get("weekly", 4),
            monthly=value.get("monthly", 6),
        )
        for name in ("daily", "weekly", "monthly"):
            number = getattr(parsed, name)
            if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number <= 365:
                raise BackupError("retention values must be between 0 and 365")
        return parsed


@dataclass(frozen=True)
class OrchestraSources:
    config_root: Path
    state_dir: Path
    releases: Path
    systemd_root: Path
    verifier: Path | None
    checkout: Path | None

    @classmethod
    def from_value(cls, value: object) -> "OrchestraSources":
        if not isinstance(value, dict):
            raise BackupError("orchestra backup settings are required")
        required = {
            "config_root": value.get("config_root"),
            "state_dir": value.get("state_dir"),
            "releases": value.get("releases"),
            "systemd_root": value.get("systemd_root", "/etc/systemd/system"),
        }
        if not all(isinstance(item, str) and item for item in required.values()):
            raise BackupError("orchestra backup paths must be non-empty strings")
        verified = value.get("verifier")
        checkout = value.get("checkout")
        if verified is not None and (not isinstance(verified, str) or not verified):
            raise BackupError("orchestra verifier path is invalid")
        if checkout is not None and (not isinstance(checkout, str) or not checkout):
            raise BackupError("orchestra checkout path is invalid")
        return cls(
            config_root=Path(required["config_root"]),
            state_dir=Path(required["state_dir"]),
            releases=Path(required["releases"]),
            systemd_root=Path(required["systemd_root"]),
            verifier=Path(verified) if verified else None,
            checkout=Path(checkout) if checkout else None,
        )


@dataclass(frozen=True)
class NextcloudSources:
    compose_file: Path
    compose_dir: Path
    app_container: str
    db_container: str
    database: str
    user: str
    html_dir: Path
    data_dir: Path
    extra_dirs: tuple[Path, ...] = ()

    @classmethod
    def from_value(cls, value: object) -> "NextcloudSources":
        if not isinstance(value, dict):
            raise BackupError("nextcloud backup settings are required")
        compose_file = value.get("compose_file")
        compose_dir = value.get("compose_dir")
        app_container = value.get("app_container", "mboxnextcloud-app-1")
        db_container = value.get("db_container", "mboxnextcloud-db-1")
        database = value.get("database", "nextcloud")
        user = value.get("user", "nextcloud")
        html_dir = value.get("html_dir")
        data_dir = value.get("data_dir")
        if not all(isinstance(item, str) and item for item in (
                compose_file, compose_dir, app_container, db_container,
                database, user, html_dir, data_dir)):
            raise BackupError("nextcloud backup settings are incomplete")
        return cls(
            compose_file=Path(compose_file),
            compose_dir=Path(compose_dir),
            app_container=app_container,
            db_container=db_container,
            database=database,
            user=user,
            html_dir=Path(html_dir),
            data_dir=Path(data_dir),
            extra_dirs=tuple(Path(item) for item in _safe_relative_paths(
                value.get("extra_dirs", []), "nextcloud extra_dirs",
            )),
        )


@dataclass(frozen=True)
class BackupConfig:
    path: Path
    repository: str
    password_file: Path
    off_device: bool
    staging_dir: Path
    status_file: Path
    lock_file: Path
    cache_dir: Path
    environment_file: Path | None
    restic_binary: str
    max_age_hours: int
    integrity_max_age_hours: int
    retention: Retention
    orchestra: OrchestraSources
    nextcloud: NextcloudSources
    keep_prune: bool = True

    @classmethod
    def load(cls, path: Path) -> "BackupConfig":
        path = Path(path)
        _require_private_file(path, "backup configuration")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BackupError("backup configuration is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise BackupError("backup configuration must be an object")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise BackupError("unsupported backup configuration schema")
        repository = raw.get("repository")
        if not isinstance(repository, str) or not repository:
            raise BackupError("backup repository is required")
        if not repository.startswith(DEFAULT_REPOSITORY_PREFIXES):
            raise BackupError("backup repository must use a reviewed restic backend")
        password_file = raw.get("password_file")
        if not isinstance(password_file, str) or not password_file:
            raise BackupError("backup password_file is required")
        off_device = raw.get("off_device", False)
        if not isinstance(off_device, bool):
            raise BackupError("off_device must be a boolean")
        environment_file = raw.get("environment_file")
        if environment_file is not None:
            if not isinstance(environment_file, str) or not environment_file:
                raise BackupError("environment_file must be a path")
            environment_file = Path(environment_file)
        restic_binary = raw.get("restic_binary", "restic")
        if not isinstance(restic_binary, str) or not restic_binary:
            raise BackupError("restic_binary must be a command name")
        max_age = raw.get("max_age_hours", 30)
        integrity_age = raw.get("integrity_max_age_hours", 192)
        for value in (max_age, integrity_age):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 8760:
                raise BackupError("backup age limits must be between 1 and 8760 hours")
        staging_dir = Path(raw.get("staging_dir", DEFAULT_STAGING))
        status_file = Path(raw.get("status_file", DEFAULT_STATUS))
        lock_file = Path(raw.get("lock_file", status_file.with_name("backup.lock")))
        cache_dir = Path(raw.get("cache_dir", DEFAULT_CACHE))
        return cls(
            path=path,
            repository=repository,
            password_file=Path(password_file),
            off_device=off_device,
            staging_dir=staging_dir,
            status_file=status_file,
            lock_file=lock_file,
            cache_dir=cache_dir,
            environment_file=environment_file,
            restic_binary=restic_binary,
            max_age_hours=max_age,
            integrity_max_age_hours=integrity_age,
            retention=Retention.from_value(raw.get("retention")),
            orchestra=OrchestraSources.from_value(raw.get("orchestra")),
            nextcloud=NextcloudSources.from_value(raw.get("nextcloud")),
            keep_prune=raw.get("prune", True) is not False,
        )

    def validate_sources(self) -> None:
        _require_private_file(self.password_file, "backup password file")
        if self.environment_file is not None:
            _read_owner_file(self.environment_file)
        _require_directory(self.orchestra.config_root, "orchestra config_root")
        _require_directory(self.orchestra.state_dir, "orchestra state_dir")
        _require_directory(self.orchestra.releases, "orchestra releases")
        _require_directory(self.orchestra.systemd_root, "orchestra systemd_root")
        if self.orchestra.verifier is not None and not self.orchestra.verifier.is_file():
            raise BackupError("orchestra verifier is unavailable")
        if self.orchestra.checkout is not None and not self.orchestra.checkout.is_dir():
            raise BackupError("orchestra checkout is unavailable")
        _require_regular_file(self.nextcloud.compose_file, "nextcloud compose_file")
        _require_directory(self.nextcloud.compose_dir, "nextcloud compose_dir")
        _require_directory(self.nextcloud.html_dir, "nextcloud html_dir")
        _require_directory(self.nextcloud.data_dir, "nextcloud data_dir")
        for path in self.nextcloud.extra_dirs:
            _require_directory(path, "nextcloud extra directory")


@dataclass
class BackupResult:
    status: str
    snapshot_id: str | None = None
    snapshot_short_id: str | None = None
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    bytes: int | None = None
    files: int | None = None
    error: dict[str, str] | None = None
    integrity: dict[str, Any] | None = None
    retention: dict[str, int] | None = None
    off_device: bool = False

    def as_status(self, config: BackupConfig) -> dict[str, Any]:
        return {
            "schema_version": STATUS_SCHEMA_VERSION,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at or utc_now(),
            "repository": config.repository,
            "off_device": self.off_device,
            "snapshot_id": self.snapshot_id,
            "snapshot_short_id": self.snapshot_short_id,
            "bytes": self.bytes,
            "files": self.files,
            "error": self.error,
            "integrity": self.integrity,
            "retention": self.retention,
        }


def _restic_environment(config: BackupConfig) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "LC_ALL", "SSH_AUTH_SOCK"}
    }
    environment.update({
        "RESTIC_REPOSITORY": config.repository,
        "RESTIC_PASSWORD_FILE": str(config.password_file),
        "RESTIC_CACHE_DIR": str(config.cache_dir),
        "GIT_TERMINAL_PROMPT": "0",
    })
    if config.environment_file is not None:
        environment.update(_read_owner_file(config.environment_file))
    return environment


def _restic_command(
    config: BackupConfig, *args: str, timeout: int = 3600,
) -> subprocess.CompletedProcess[str]:
    command = [config.restic_binary, *args]
    try:
        completed = subprocess.run(
            command, env=_restic_environment(config), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            check=False, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError("restic command could not run") from exc
    if completed.returncode != 0:
        raise BackupError(f"restic {args[0] if args else 'command'} failed")
    return completed


def _restic_json(
    config: BackupConfig, *args: str, timeout: int = 3600,
) -> Any:
    completed = _restic_command(config, *args, timeout=timeout)
    values = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            values.append(json.loads(line))
        except ValueError as exc:
            raise BackupError("restic returned invalid JSON") from exc
    if not values:
        return {}
    return values[-1] if len(values) == 1 else values


def _restic(config: BackupConfig, *args: str, timeout: int = 3600) -> Any:
    return _restic_json(config, *args, timeout=timeout)


def _docker(config: BackupConfig, *args: str, timeout: int = 300) -> str:
    command = ["docker", *args]
    try:
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError("docker command could not run") from exc
    if completed.returncode != 0:
        raise BackupError(f"docker {args[0] if args else 'command'} failed")
    return completed.stdout


def _maintenance(config: BackupConfig, enabled: bool) -> None:
    _docker(
        config, "exec", "-u", "www-data", config.nextcloud.app_container,
        "php", "occ", "maintenance:mode", "--on" if enabled else "--off",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise BackupError("backup manifest source is unavailable") from exc
    return digest.hexdigest()


def _manifest(
    config: BackupConfig, dump_path: Path, source_paths: Iterable[Path],
) -> dict[str, Any]:
    selected = [
        config.orchestra.config_root / "omcp.json",
        config.nextcloud.compose_file,
        config.nextcloud.compose_dir / ".env",
    ]
    files = []
    for path in selected:
        if path.is_file() and not path.is_symlink():
            files.append({
                "path": str(path), "size": path.stat().st_size,
                "sha256": _sha256(path),
            })
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "host": os.uname().nodename,
        "repository": config.repository,
        "off_device": config.off_device,
        "source_paths": [str(path) for path in source_paths],
        "nextcloud_dump": {
            "path": str(dump_path), "size": dump_path.stat().st_size,
            "sha256": _sha256(dump_path),
        },
        "files": files,
    }


def _source_paths(
    config: BackupConfig, dump_path: Path, manifest_path: Path,
) -> list[Path]:
    orchestra = config.orchestra
    paths = [
        orchestra.config_root,
        orchestra.state_dir / "state.json",
        orchestra.state_dir / "audit.jsonl",
        orchestra.state_dir / "tasks",
        orchestra.state_dir / "security-probes",
        orchestra.releases,
        orchestra.systemd_root,
        dump_path,
        manifest_path,
    ]
    if orchestra.verifier is not None:
        paths.append(orchestra.verifier)
    if orchestra.checkout is not None:
        paths.append(orchestra.checkout)
    paths.extend([config.nextcloud.compose_file, config.nextcloud.compose_dir / ".env",
                  config.nextcloud.html_dir, config.nextcloud.data_dir])
    paths.extend(config.nextcloud.extra_dirs)
    return [path for path in paths if path.exists()]


def _restic_init_if_needed(config: BackupConfig) -> None:
    try:
        _restic_command(config, "cat", "config", timeout=60)
    except BackupError:
        _restic_command(config, "init", timeout=120)


def _write_status(config: BackupConfig, value: dict[str, Any]) -> None:
    # The status contains no credentials, prompts or task text.  It is
    # world-readable so the existing unprivileged health service can report
    # failure/recency while the backup service remains root.
    _atomic_json(
        config.status_file, value, mode=0o644, directory_mode=0o755,
    )


def create_backup(
    config: BackupConfig, *, maintenance: bool = True, now: str | None = None,
) -> BackupResult:
    """Create one restic snapshot containing Orchestra and Nextcloud state."""
    started = now or utc_now()
    result = BackupResult(
        status="running", started_at=started, off_device=config.off_device,
        retention={
            "daily": config.retention.daily, "weekly": config.retention.weekly,
            "monthly": config.retention.monthly,
        },
    )
    lock_path = config.lock_file
    lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(lock_path.parent, 0o700)
    lock = lock_path.open("a+")
    staging: Path | None = None
    maintenance_enabled = False
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("another backup is already running") from exc
        config.validate_sources()
        _ensure_private_directory(config.staging_dir, "backup staging_dir")
        _ensure_private_directory(config.cache_dir, "backup cache_dir")
        _restic_init_if_needed(config)
        staging = Path(tempfile.mkdtemp(prefix="snapshot-", dir=config.staging_dir))
        os.chmod(staging, 0o700)
        dump_path = staging / "nextcloud.dump"
        if maintenance:
            _maintenance(config, True)
            maintenance_enabled = True
        try:
            dump = subprocess.run(
                ["docker", "exec", config.nextcloud.db_container,
                 "pg_dump", "-U", config.nextcloud.user,
                 "-d", config.nextcloud.database, "--format=custom"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False, timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackupError("Nextcloud database dump could not run") from exc
        if dump.returncode != 0 or not dump.stdout:
            raise BackupError("Nextcloud database dump failed")
        dump_path.write_bytes(dump.stdout)
        os.chmod(dump_path, 0o600)
        manifest_path = staging / "orchestra-backup-manifest.json"
        sources = _source_paths(config, dump_path, manifest_path)
        manifest = _manifest(config, dump_path, sources)
        _atomic_json(manifest_path, manifest)
        # The manifest itself is part of the snapshot; add its path after it
        # has been written and include the dump in the same snapshot.
        sources.append(manifest_path)
        snapshot_lines = _restic_json(
            config, "backup", "--json", "--tag", "orchestra", "--tag", "nextcloud",
            "--host", "pixelbox", *[str(path) for path in sources],
            timeout=7200,
        )
        snapshot = snapshot_lines[-1] if isinstance(snapshot_lines, list) else snapshot_lines
        if maintenance_enabled:
            _maintenance(config, False)
            maintenance_enabled = False
        snapshot_id = snapshot.get("snapshot_id") if isinstance(snapshot, dict) else None
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise BackupError("restic did not return a snapshot id")
        summary = snapshot.get("summary") if isinstance(snapshot, dict) else {}
        if not isinstance(summary, dict) or not summary:
            summary = snapshot if isinstance(snapshot, dict) else {}
        result.snapshot_id = snapshot_id
        result.snapshot_short_id = snapshot_id[:8]
        result.bytes = (
            summary.get("data_added") if isinstance(summary, dict) else None
        )
        result.files = (
            summary.get("files_new") if isinstance(summary, dict) else None
        )
        if config.keep_prune:
            retention = [
                "--keep-daily", str(config.retention.daily),
                "--keep-weekly", str(config.retention.weekly),
                "--keep-monthly", str(config.retention.monthly),
            ]
            _restic_command(config, "forget", "--prune", *retention, timeout=7200)
        # Integrity is checked in a separate bounded pass; this proves the
        # repository and snapshot metadata are readable now.
        _restic_command(config, "check", "--read-data-subset=10%", timeout=7200)
        result.integrity = {
            "status": "healthy", "checked_at": utc_now(),
            "snapshot_id": snapshot_id, "read_data_subset": "10%",
        }
        result.status = "success"
        result.finished_at = utc_now()
        _write_status(config, result.as_status(config))
        return result
    except BaseException as exc:
        if maintenance_enabled:
            try:
                _maintenance(config, False)
            except BackupError:
                pass
        result.status = "failed"
        result.error = _bounded_error(exc)
        result.finished_at = utc_now()
        try:
            _write_status(config, result.as_status(config))
        except OSError:
            pass
        raise
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock.close()


def _status_age(status: dict[str, Any]) -> float | None:
    value = status.get("finished_at")
    if not isinstance(value, str):
        return None
    try:
        then = datetime.fromisoformat(value)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - then).total_seconds()
    return max(0.0, age)


def backup_health(config_path: Path | None, *, now: float | None = None) -> dict[str, Any]:
    """Read-only backup health; never runs restic or a timer check."""
    if config_path is None:
        return {
            "checked": False, "status": "unchecked",
            "reason": "backup_config_not_configured",
            "next_action": "Choose and configure an off-device restic repository.",
        }
    try:
        config = BackupConfig.load(Path(config_path))
    except BackupError as exc:
        return {
            "checked": True, "status": "failed", "reason": "invalid_backup_config",
            "error_type": type(exc).__name__,
            "next_action": "Repair the owner-only backup configuration.",
        }
    try:
        raw = json.loads(config.status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "checked": False, "status": "unchecked", "reason": "no_backup_status",
            "repository": config.repository, "off_device": config.off_device,
            "next_action": "Run a verified backup and restore test.",
        }
    if not isinstance(raw, dict) or raw.get("schema_version") != STATUS_SCHEMA_VERSION:
        return {
            "checked": True, "status": "failed", "reason": "invalid_backup_status",
            "repository": config.repository, "off_device": config.off_device,
            "next_action": "Repair or remove the invalid backup status.",
        }
    status = raw.get("status")
    age = _status_age(raw)
    current_time = time.time() if now is None else now
    age = (
        max(0.0, current_time - datetime.fromisoformat(raw["finished_at"]).timestamp())
        if isinstance(raw.get("finished_at"), str) else age
    )
    result: dict[str, Any] = {
        "checked": True, "status": status, "repository": config.repository,
        "off_device": config.off_device, "snapshot_id": raw.get("snapshot_id"),
        "snapshot_short_id": raw.get("snapshot_short_id"),
        "last_success": raw.get("finished_at") if status == "success" else None,
        "age_seconds": round(age, 1) if age is not None else None,
        "integrity": raw.get("integrity"),
    }
    if not config.off_device:
        result.update({
            "status": "unchecked", "reason": "off_device_not_configured",
            "next_action": "Choose an off-device destination and key custody; "
                           "a local or attached-Mbox copy is not host-loss protection.",
        })
        return result
    if status != "success":
        result.update({
            "status": "failed",
            "reason": "last_backup_failed" if status == "failed" else "no_successful_backup",
            "first_observed": raw.get("started_at"),
            "last_observed": raw.get("finished_at"),
            "operator_action": "Inspect the backup status and rerun the backup.",
        })
        return result
    max_age = config.max_age_hours * 3600
    if age is None or age > max_age:
        result.update({
            "status": "failed", "reason": "backup_overdue",
            "first_observed": raw.get("finished_at"),
            "last_observed": raw.get("finished_at"),
            "operator_action": "Run the backup and investigate the timer/service.",
        })
        return result
    integrity = raw.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("status") != "healthy":
        result.update({
            "status": "failed", "reason": "integrity_not_checked",
            "operator_action": "Run restic check and record its result.",
        })
        return result
    checked_at = integrity.get("checked_at")
    try:
        integrity_age = current_time - datetime.fromisoformat(checked_at).timestamp()
    except (TypeError, ValueError):
        integrity_age = None
    if integrity_age is None or integrity_age > config.integrity_max_age_hours * 3600:
        result.update({
            "status": "failed", "reason": "integrity_overdue",
            "operator_action": "Run restic check and record its result.",
        })
        return result
    result.update({"status": "healthy", "reason": "verified_recent_snapshot"})
    return result


def _snapshot_id(config: BackupConfig, requested: str) -> str:
    if requested not in {"latest", ""}:
        return requested
    values = _restic_json(config, "snapshots", "--json", timeout=120)
    snapshots = values if isinstance(values, list) else values.get("snapshots", [])
    if not snapshots:
        raise BackupError("no restic snapshots are available")
    snapshots.sort(key=lambda item: item.get("time", ""))
    snapshot_id = snapshots[-1].get("short_id") or snapshots[-1].get("id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise BackupError("latest restic snapshot has no id")
    return snapshot_id


def restore_snapshot(config: BackupConfig, snapshot: str, target: Path) -> dict[str, Any]:
    config.validate_sources()
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise BackupError("restore target must be empty")
    if target.resolve() == Path("/") or str(target.resolve()).startswith(
            str(config.orchestra.state_dir.resolve()) + os.sep):
        raise BackupError("restore target must be isolated from production")
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)
    snapshot_id = _snapshot_id(config, snapshot)
    _restic_command(config, "restore", snapshot_id, "--target", str(target), timeout=7200)
    manifest = None
    value: dict[str, Any] | None = None
    candidates = list(target.rglob("orchestra-backup-manifest.json"))
    candidates.extend(target.rglob("manifest.json"))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(parsed, dict) and parsed.get("schema_version") == SCHEMA_VERSION
                and isinstance(parsed.get("nextcloud_dump"), dict)):
            manifest, value = candidate, parsed
            break
    if manifest is None or value is None:
        raise BackupError("restored snapshot has no manifest")
    for item in value.get("files", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise BackupError("restored manifest file entry is invalid")
        source_path = item["path"]
        # state.json and audit.jsonl are live, append-oriented files and are
        # intentionally not hashed in the manifest; verify their structure
        # below instead of treating normal coordinator writes as corruption.
        if source_path.endswith("/state.json") or source_path.endswith("/audit.jsonl"):
            continue
        restored = target / source_path.lstrip("/")
        if not restored.is_file() or _sha256(restored) != item.get("sha256"):
            raise BackupError(f"restored file failed content verification: {source_path}")
    state_files = [
        path for path in target.rglob("state.json")
        if path.is_file() and path.parent.name == "state"
    ]
    if not state_files:
        raise BackupError("restored Orchestra state.json is missing")
    try:
        restored_state = json.loads(state_files[0].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError("restored Orchestra state.json is invalid") from exc
    if not isinstance(restored_state.get("tasks"), dict):
        raise BackupError("restored Orchestra state.json has no tasks map")
    dump = value.get("nextcloud_dump")
    if isinstance(dump, dict):
        restored_dump = target / str(dump["path"]).lstrip("/")
        if not restored_dump.is_file() or restored_dump.stat().st_size != dump.get("size"):
            raise BackupError("restored Nextcloud dump is missing or truncated")
    return {
        "status": "healthy", "snapshot_id": snapshot_id,
        "target": str(target), "manifest": str(manifest),
        "files_verified": len(value.get("files", [])),
    }


def check_integrity(config: BackupConfig) -> dict[str, Any]:
    _restic_command(config, "check", "--read-data-subset=10%", timeout=7200)
    value = {"status": "healthy", "checked_at": utc_now(), "read_data_subset": "10%"}
    try:
        status = json.loads(config.status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    if isinstance(status, dict) and status.get("schema_version") == STATUS_SCHEMA_VERSION:
        status["integrity"] = value
        _write_status(config, status)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or restore Orchestra backups")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--no-maintenance", action="store_true")
    check = sub.add_parser("check")
    restore = sub.add_parser("restore")
    restore.add_argument("--snapshot", default="latest")
    restore.add_argument("--target", type=Path, required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(backup_health(args.config), indent=2, sort_keys=True))
            return 0
        config = BackupConfig.load(args.config)
        if args.command == "run":
            result = create_backup(config, maintenance=not args.no_maintenance)
            print(json.dumps(result.as_status(config), indent=2, sort_keys=True))
            return 0
        if args.command == "check":
            print(json.dumps(check_integrity(config), indent=2, sort_keys=True))
            return 0
        if args.command == "restore":
            print(json.dumps(restore_snapshot(config, args.snapshot, args.target),
                             indent=2, sort_keys=True))
            return 0
    except BackupError as exc:
        print(json.dumps(_bounded_error(exc), sort_keys=True))
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
