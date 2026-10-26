"""Tests for the fail-closed PixelBox activation config preflight."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.pixelbox import config_preflight


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = config_preflight.REQUIRED_RELEASE_FILES


def _config(base: Path) -> tuple[Path, Path]:
    root = base / "project"
    root.mkdir()
    state = base / "state"
    state.mkdir(mode=0o700)
    config_root = base / "etc"
    config_root.mkdir(mode=0o710)
    config = config_root / "omcp.json"
    config.write_text(json.dumps({
        "repository": "owner/repo", "project_root": str(root),
        "state_dir": str(state), "workers": {"ina": {"command": ["codex"]}},
        "principal": {"identity": "chatgpt:test", "permissions": ["status:read"]},
    }))
    config.chmod(0o600)
    return config, config_root


def _release(base: Path) -> Path:
    release = base / "release"
    release.mkdir()
    for relative in REQUIRED:
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(ROOT / relative)
    return release


class ConfigPreflightTests(unittest.TestCase):
    def _run(self, config: Path, config_root: Path, release: Path, *extra: str):
        return config_preflight.main([
            "--config", str(config), "--config-root", str(config_root),
            "--release-root", str(release), "--service-user", __import__("getpass").getuser(),
            *extra,
        ])

    def test_valid_config_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config, config_root = _config(base)
            self.assertEqual(self._run(config, config_root, ROOT), 0)

    def test_wrong_owner_fails_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config, config_root = _config(base)
            with patch.object(config_preflight, "_service_user") as user:
                user.return_value = type("User", (), {
                    "pw_name": "root", "pw_uid": 0, "pw_gid": 0,
                })()
                self.assertEqual(self._run(config, config_root, ROOT), 1)

    def test_public_mode_and_invalid_json_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config, config_root = _config(base)
            config.chmod(0o644)
            self.assertEqual(self._run(config, config_root, ROOT), 1)
            config.chmod(0o600)
            config.write_text("{not-json")
            self.assertEqual(self._run(config, config_root, ROOT), 1)

    def test_low_space_and_missing_release_file_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config, config_root = _config(base)
            with patch("deploy.pixelbox.config_preflight.shutil.disk_usage") as usage:
                usage.return_value = type("Usage", (), {"free": 0})()
                self.assertEqual(self._run(config, config_root, ROOT), 1)
            release = _release(base)
            (release / REQUIRED[0]).unlink()
            self.assertEqual(self._run(config, config_root, release), 1)

    def test_release_marker_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config, config_root = _config(base)
            release = base / "release"
            release.mkdir()
            for relative in REQUIRED:
                target = release / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(ROOT / relative)
            (release / ".orchestra-release-sha").write_text("a" * 40)
            self.assertEqual(self._run(
                config, config_root, release, "--expected-sha", "b" * 40,
            ), 1)


if __name__ == "__main__":
    unittest.main()
