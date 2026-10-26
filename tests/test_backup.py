"""Tests for the encrypted Orchestra/Nextcloud backup contract."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from omcp.backup import (
    BackupConfig, BackupError, backup_health, check_integrity, create_backup,
    restore_snapshot,
)


def _write_private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def _config(root: Path, *, off_device: bool = True, status: dict | None = None,
            age_hours: float = 1, integrity_age_hours: float = 1) -> tuple[Path, Path, Path]:
    config_root = root / "etc"
    state = root / "state"
    releases = root / "releases"
    systemd = root / "systemd"
    html = root / "html"
    data = root / "data"
    compose_dir = root / "nextcloud"
    staging = root / "staging"
    cache = root / "cache"
    backup_root = root / "backup"
    for path in (config_root, state, releases, systemd, html, data, compose_dir,
                 staging, cache, backup_root):
        path.mkdir(parents=True, exist_ok=True)
    (state / "state.json").write_text(json.dumps({"tasks": {}}))
    (state / "state.json").chmod(0o600)
    (state / "audit.jsonl").write_text("")
    (state / "audit.jsonl").chmod(0o600)
    (state / "tasks").mkdir(exist_ok=True)
    (state / "security-probes").mkdir(exist_ok=True)
    (config_root / "omcp.json").write_text("{}")
    (config_root / "omcp.json").chmod(0o600)
    (compose_dir / "compose.yaml").write_text("services: {}\n")
    (compose_dir / ".env").write_text("DB_PASSWORD=not-logged\n")
    (compose_dir / ".env").chmod(0o600)
    password = backup_root / "password"
    _write_private(password, "test-password\n")
    status_file = backup_root / "status.json"
    config_path = root / "backup.json"
    value = {
        "schema_version": 1,
        "repository": "local:" + str(root / "repo"),
        "password_file": str(password),
        "off_device": off_device,
        "staging_dir": str(staging),
        "status_file": str(status_file),
        "lock_file": str(backup_root / "backup.lock"),
        "cache_dir": str(cache),
        "max_age_hours": 30,
        "integrity_max_age_hours": 192,
        "retention": {"daily": 7, "weekly": 4, "monthly": 6},
        "orchestra": {
            "config_root": str(config_root), "state_dir": str(state),
            "releases": str(releases), "systemd_root": str(systemd),
        },
        "nextcloud": {
            "compose_file": str(compose_dir / "compose.yaml"),
            "compose_dir": str(compose_dir),
            "app_container": "app", "db_container": "db",
            "database": "nextcloud", "user": "nextcloud",
            "html_dir": str(html), "data_dir": str(data),
        },
    }
    config_path.write_text(json.dumps(value))
    config_path.chmod(0o600)
    if status is not None:
        status_file.write_text(json.dumps(status))
        status_file.chmod(0o644)
    return config_path, status_file, backup_root


def _success_status(*, age_hours: float = 1, integrity_age_hours: float = 1,
                    off_device: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "status": "success",
        "started_at": (now - timedelta(hours=age_hours + 1)).isoformat(),
        "finished_at": (now - timedelta(hours=age_hours)).isoformat(),
        "repository": "local:/repo",
        "off_device": off_device,
        "snapshot_id": "a" * 64,
        "snapshot_short_id": "aaaaaaaa",
        "integrity": {
            "status": "healthy",
            "checked_at": (now - timedelta(hours=integrity_age_hours)).isoformat(),
        },
    }


class BackupConfigurationTests(unittest.TestCase):
    def test_loads_private_config_and_accepts_local_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = _config(Path(directory), off_device=False)
            config = BackupConfig.load(path)
            self.assertEqual(config.repository, "local:" + str(Path(directory) / "repo"))
            self.assertFalse(config.off_device)
            config.validate_sources()

    def test_rejects_public_config_and_password(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = _config(root)
            path.chmod(0o644)
            with self.assertRaisesRegex(BackupError, "configuration"):
                BackupConfig.load(path)
            path.chmod(0o600)
            config = BackupConfig.load(path)
            config.password_file.chmod(0o644)
            with self.assertRaisesRegex(BackupError, "password"):
                config.validate_sources()

    def test_rejects_unreviewed_backend_and_invalid_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = _config(Path(directory))
            raw = json.loads(path.read_text())
            raw["repository"] = "http://example.invalid/repo"
            path.write_text(json.dumps(raw))
            path.chmod(0o600)
            with self.assertRaisesRegex(BackupError, "backend"):
                BackupConfig.load(path)
            raw["repository"] = "local:/repo"
            raw["retention"] = {"daily": 999}
            path.write_text(json.dumps(raw))
            path.chmod(0o600)
            with self.assertRaisesRegex(BackupError, "retention"):
                BackupConfig.load(path)


class BackupHealthTests(unittest.TestCase):
    def test_no_status_is_unchecked_not_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = _config(Path(directory))
            status = backup_health(path)
            self.assertEqual(status["status"], "unchecked")
            self.assertFalse(status["checked"])

    def test_local_only_success_is_not_off_device_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = _config(
                Path(directory), off_device=False,
                status=_success_status(off_device=False),
            )
            status = backup_health(path)
            self.assertEqual(status["status"], "unchecked")
            self.assertEqual(status["reason"], "off_device_not_configured")

    def test_recent_success_and_integrity_are_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = _config(Path(directory), status=_success_status())
            status = backup_health(path)
            self.assertEqual(status["status"], "healthy")
            self.assertTrue(status["checked"])

    def test_stale_backup_is_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = _config(
                Path(directory), status=_success_status(age_hours=31),
            )
            status = backup_health(path)
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["reason"], "backup_overdue")

    def test_failed_last_run_is_failed_even_with_old_success(self):
        with tempfile.TemporaryDirectory() as directory:
            status = _success_status(age_hours=1)
            status["status"] = "failed"
            status["error"] = {"type": "BackupError", "message": "failed"}
            path, _, _ = _config(Path(directory), status=status)
            result = backup_health(path)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "last_backup_failed")

    def test_missing_or_stale_integrity_is_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = _config(
                Path(directory), status=_success_status(integrity_age_hours=200),
            )
            self.assertEqual(backup_health(path)["status"], "failed")
            status = _success_status()
            del status["integrity"]
            status_path = Path(directory) / "backup" / "status.json"
            status_path.write_text(json.dumps(status))
            status_path.chmod(0o644)
            self.assertEqual(backup_health(path)["status"], "failed")


class BackupCommandTests(unittest.TestCase):
    def _fake_tools(self, root: Path) -> tuple[Path, Path, Path]:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        restic = bin_dir / "restic"
        docker = bin_dir / "docker"
        docker_log = root / "docker.log"
        restic.write_text("""#!/bin/sh
case "$1" in
  cat) exit 0 ;;
  init) exit 0 ;;
  backup) printf '%s\\n' '{"snapshot_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","summary":{"data_added":1234,"files_new":5}}' ;;
  forget|check) exit 0 ;;
  snapshots) printf '%s\\n' '[]' ;;
  *) exit 1 ;;
esac
""")
        restic.chmod(0o700)
        docker.write_text("""#!/bin/sh
case "$*" in
  *"pg_dump"*) printf 'fake dump body'; exit 0 ;;
  *"maintenance:mode --on"*) printf 'on\\n' >> "$DOCKER_LOG" ;;
  *"maintenance:mode --off"*) printf 'off\\n' >> "$DOCKER_LOG" ;;
esac
exit 0
""")
        docker.chmod(0o700)
        return restic, docker, docker_log

    def test_create_backup_writes_success_and_maintenance_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, status_path, _ = _config(root)
            restic, docker, docker_log = self._fake_tools(root)
            raw = json.loads(path.read_text())
            raw["restic_binary"] = str(restic)
            path.write_text(json.dumps(raw))
            path.chmod(0o600)
            env = {"PATH": str(root / "bin"), "DOCKER_LOG": str(docker_log)}
            with patch.dict(os.environ, env, clear=False):
                result = create_backup(BackupConfig.load(path))
            self.assertEqual(result.status, "success")
            self.assertEqual(result.snapshot_short_id, "aaaaaaaa")
            status = json.loads(status_path.read_text())
            self.assertEqual(status["status"], "success")
            self.assertEqual(status["integrity"]["status"], "healthy")
            self.assertEqual(docker_log.read_text(), "on\noff\n")

    def test_failed_restic_run_records_failure_and_turns_maintenance_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, status_path, _ = _config(root)
            restic, docker, docker_log = self._fake_tools(root)
            restic.write_text("""#!/bin/sh
case "$1" in
  cat) exit 0 ;;
  init) exit 0 ;;
  backup) exit 1 ;;
esac
""")
            restic.chmod(0o700)
            raw = json.loads(path.read_text())
            raw["restic_binary"] = str(restic)
            path.write_text(json.dumps(raw))
            path.chmod(0o600)
            env = {"PATH": str(root / "bin"), "DOCKER_LOG": str(docker_log)}
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaises(BackupError):
                    create_backup(BackupConfig.load(path))
            status = json.loads(status_path.read_text())
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["error"]["type"], "BackupError")
            self.assertEqual(docker_log.read_text(), "on\noff\n")

    def test_integrity_check_updates_existing_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, status_path, _ = _config(root, status=_success_status())
            restic, _, _ = self._fake_tools(root)
            raw = json.loads(path.read_text())
            raw["restic_binary"] = str(restic)
            path.write_text(json.dumps(raw))
            path.chmod(0o600)
            result = check_integrity(BackupConfig.load(path))
            self.assertEqual(result["status"], "healthy")
            self.assertEqual(json.loads(status_path.read_text())["integrity"]["status"], "healthy")

    def test_restore_rejects_production_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = _config(root)
            config = BackupConfig.load(path)
            with self.assertRaisesRegex(BackupError, "isolated"):
                restore_snapshot(config, "latest", config.orchestra.state_dir / "restore")


if __name__ == "__main__":
    unittest.main()
