"""Adversarial checks for the inactive fixed-profile PixelBox test launcher."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra import pixelbox_test_launcher as launcher


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "pixelbox" / "systemd" / "orchestra-test@.service.example"


class PixelBoxTestLauncherTests(unittest.TestCase):
    sha = "a" * 40

    def release(self, directory: Path, *, marker: str | None = None) -> tuple[Path, Path]:
        releases = directory / "releases"
        target = releases / self.sha
        (target / "tests").mkdir(parents=True)
        (target / ".orchestra-release-sha").write_text(marker or self.sha)
        (target / ".orchestra-release-digest").write_text("b" * 64)
        for path in (releases, target, target / ".orchestra-release-sha", target / ".orchestra-release-digest"):
            path.chmod(0o755 if path.is_dir() else 0o444)
        return releases, target

    def protected_release(self, directory: Path) -> tuple[Path, Path]:
        releases, target = self.release(directory)
        return releases, target

    def test_refuses_non_exact_sha_and_mutable_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            releases, target = self.release(Path(temporary))
            with patch.object(launcher, "_protected", return_value=True):
                with self.assertRaisesRegex(launcher.LaunchRefused, "exactly"):
                    launcher.verified_release(releases, "A" * 40)
                (target / ".orchestra-release-sha").chmod(0o644)
                (target / ".orchestra-release-sha").write_text("c" * 40)
                (target / ".orchestra-release-sha").chmod(0o444)
                with self.assertRaisesRegex(launcher.LaunchRefused, "does not match"):
                    launcher.verified_release(releases, self.sha)

    def test_rejects_current_pointer_socket_and_group_writable_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases, target = self.release(root)
            with patch.object(launcher, "_protected", return_value=True):
                (releases / "current").symlink_to(target)
                with self.assertRaises(launcher.LaunchRefused):
                    launcher.verified_release(releases, "current")
                target.chmod(0o775)
                with patch.object(launcher, "_protected", side_effect=lambda info: not bool(info.st_mode & 0o022)):
                    with self.assertRaisesRegex(launcher.LaunchRefused, "protected"):
                        launcher.verified_release(releases, self.sha)

    def test_fixed_command_has_no_caller_controlled_arguments(self):
        release = launcher.Release(self.sha, Path("/irrelevant"), "b" * 64)
        self.assertEqual(launcher.fixed_command(release), launcher.FIXED_COMMAND)
        self.assertIn("unittest", launcher.FIXED_COMMAND)
        self.assertNotIn("sh", launcher.FIXED_COMMAND)

    def test_timeout_is_bounded_and_records_sha_bound_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases, _target = self.release(root)
            result = root / "results" / "result.json"
            sleeping = (sys.executable, "-c", "import time; time.sleep(5)")
            started = time.monotonic()
            with patch.object(launcher, "_protected", return_value=True), patch.object(
                launcher, "FIXED_COMMAND", sleeping
            ):
                evidence = launcher.run_fixed_profile(releases, self.sha, result, 1)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(evidence.outcome, "timed_out")
            stored = json.loads(result.read_text())
            self.assertEqual(stored["sha"], self.sha)
            self.assertEqual(stored["profile"], launcher.PROFILE)
            self.assertEqual(stored["exit_code"], 124)
            self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)

    def test_result_is_bounded_and_cannot_be_an_authority_grant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases, _target = self.release(root)
            result = root / "result.json"
            noisy = (sys.executable, "-c", "print('x' * 100000)")
            with patch.object(launcher, "_protected", return_value=True), patch.object(
                launcher, "FIXED_COMMAND", noisy
            ):
                evidence = launcher.run_fixed_profile(releases, self.sha, result, 10)
            self.assertEqual(evidence.outcome, "passed")
            self.assertLessEqual(len(evidence.output.encode()), launcher.RESULT_LIMIT)
            self.assertEqual(set(json.loads(result.read_text())), {
                "duration_ms", "exit_code", "outcome", "output", "output_sha256",
                "profile", "release_digest", "sha", "version",
            })

    def test_unit_defense_in_depth_blocks_network_secrets_and_host_services(self):
        unit = UNIT.read_text()
        for directive in (
            "User=orchestra-test", "SupplementaryGroups=", "PrivateNetwork=yes",
            "IPAddressDeny=any", "SocketBindDeny=any", "RestrictAddressFamilies=AF_UNIX",
            "PrivateUsers=yes", "ProtectSystem=strict", "ProtectHome=tmpfs",
            "NoNewPrivileges=yes", "CapabilityBoundingSet=", "MemoryMax=1G",
            "TasksMax=128", "CPUQuota=200%", "KillMode=control-group",
            "DEEPSEEK_API_KEY", "SSH_AUTH_SOCK", "/etc/orchestra", "/run", "/var/run", "/run/docker.sock",
            "/run/tailscale", "/var/www/nextcloud",
        ):
            self.assertIn(directive, unit)
        self.assertNotIn("EnvironmentFile=", unit)
        self.assertNotIn("/opt/orchestra/current", unit.split("[Service]", 1)[1])

    def test_unit_requires_a_root_owned_external_launcher_and_fixed_sha_instance(self):
        unit = UNIT.read_text()
        self.assertIn("/usr/local/libexec/orchestra-test-launcher", unit)
        self.assertIn("--sha %i", unit)
        self.assertIn("ConditionPathExists=/opt/orchestra/releases/%i/.orchestra-release-sha", unit)
        self.assertNotIn("omcp", unit.lower().split("[service]", 1)[1])


if __name__ == "__main__":
    unittest.main()
