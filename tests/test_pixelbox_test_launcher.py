"""Security-boundary tests for the inactive fixed-manifest PixelBox canary."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra import pixelbox_test_launcher as launcher


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "pixelbox" / "systemd" / "orchestra-test@.service.example"
MANIFEST = ROOT / "deploy" / "pixelbox" / "test-manifest.json"
CANARY = ROOT / "tests" / "test_socket_free_canary.py"


class PixelBoxTestLauncherTests(unittest.TestCase):
    sha = "a" * 40

    def release(self, parent: Path) -> tuple[Path, Path]:
        releases, root = parent / "releases", parent / "releases" / self.sha
        (root / "deploy" / "pixelbox").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "deploy" / "pixelbox" / "test-manifest.json").write_bytes(MANIFEST.read_bytes())
        (root / "tests" / "test_socket_free_canary.py").write_bytes(CANARY.read_bytes())
        (root / ".orchestra-release-sha").write_text(self.sha)
        (root / ".orchestra-release-digest").write_text(launcher._release_digest(root))
        return releases, root

    def root_context(self):
        return patch.object(launcher, "_protected", return_value=True), patch.object(
            launcher.os, "geteuid", return_value=0)

    def test_prepare_seals_exact_sha_manifest_release_and_test_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); releases, _ = self.release(base); jobs = base / "jobs"; jobs.mkdir()
            protected, root = self.root_context()
            with protected, root:
                job = launcher.prepare_job(releases, self.sha, jobs)
            value = json.loads(job.read_text())
            self.assertEqual(value["sha"], self.sha)
            self.assertEqual(value["profile"], launcher.PROFILE)
            self.assertEqual(value["command"], list(launcher.FIXED_COMMAND))
            self.assertEqual(stat.S_IMODE(job.stat().st_mode), 0o600)

    def test_prepare_refuses_mutated_release_digest_and_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); releases, release = self.release(base); jobs = base / "jobs"; jobs.mkdir()
            (release / "tests" / "test_socket_free_canary.py").write_text("tampered")
            protected, root = self.root_context()
            with protected, root, self.assertRaisesRegex(launcher.LaunchRefused, "digest"):
                launcher.prepare_job(releases, self.sha, jobs)
            (release / "deploy" / "pixelbox" / "test-manifest.json").write_text("{}")
            (release / ".orchestra-release-digest").write_text(launcher._release_digest(release))
            with protected, root, self.assertRaisesRegex(launcher.LaunchRefused, "approved socket-free"):
                launcher.prepare_job(releases, self.sha, jobs)

    def test_receipt_rechecks_release_and_requires_manager_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); releases, release = self.release(base)
            jobs, receipts, output = base / "jobs", base / "receipts", base / "output.log"
            jobs.mkdir(); receipts.mkdir(); output.write_bytes(b"untrusted test output")
            protected, root = self.root_context()
            with protected, root:
                job = launcher.prepare_job(releases, self.sha, jobs)
                with patch.object(launcher, "_unit_outcome", return_value=("failed", 1)), \
                     self.assertRaisesRegex(launcher.LaunchRefused, "did not complete"):
                    launcher.record_receipt(releases, job, output, receipts)
                with patch.object(launcher, "_unit_outcome", return_value=("success", 0)):
                    receipt = launcher.record_receipt(releases, job, output, receipts)
            value = json.loads(receipt.read_text())
            self.assertEqual(value["output_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            (release / "tests" / "test_socket_free_canary.py").write_text("tampered")
            with protected, root, patch.object(launcher, "_unit_outcome", return_value=("success", 0)), \
                 self.assertRaisesRegex(launcher.LaunchRefused, "digest"):
                launcher.record_receipt(releases, job, output, receipts)

    def test_receipt_directory_and_output_must_be_root_protected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); releases, _ = self.release(base)
            jobs, receipts, output = base / "jobs", base / "receipts", base / "output.log"
            jobs.mkdir(); receipts.mkdir(); output.write_bytes(b"x")
            with patch.object(launcher, "_protected", return_value=True), patch.object(
                launcher.os, "geteuid", return_value=0
            ):
                job = launcher.prepare_job(releases, self.sha, jobs)
            with patch.object(launcher, "_protected", return_value=False), patch.object(
                launcher.os, "geteuid", return_value=0
            ), patch.object(launcher, "_unit_outcome", return_value=("success", 0)), \
                 self.assertRaisesRegex(launcher.LaunchRefused, "unsafe file|unsafe directory"):
                launcher.record_receipt(releases, job, output, receipts)

    def test_direct_unprivileged_runner_rejects_root_and_executes_only_sealed_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); releases, _ = self.release(base); jobs = base / "jobs"; jobs.mkdir()
            with patch.object(launcher, "_protected", return_value=True), patch.object(
                launcher.os, "geteuid", return_value=0
            ):
                job = launcher.prepare_job(releases, self.sha, jobs)
            with patch.object(launcher.os, "geteuid", return_value=0), self.assertRaisesRegex(
                launcher.LaunchRefused, "must not run as root"
            ):
                launcher.run_job(job, releases)
            with patch.object(launcher, "_protected", return_value=True), patch.object(
                launcher.os, "geteuid", return_value=os.getuid()
            ):
                self.assertEqual(launcher.run_job(job, releases), 0)

    def test_unit_starts_directly_unprivileged_and_denies_all_socket_families(self):
        unit = UNIT.read_text()
        self.assertIn("User=orchestra-test", unit)
        self.assertNotIn("User=root", unit)
        self.assertIn("RestrictAddressFamilies=none", unit)
        self.assertNotIn("AF_UNIX", unit)
        self.assertIn("PrivateNetwork=yes", unit)
        self.assertIn("LimitFSIZE=32768", unit)
        self.assertIn("/var/lib/orchestra-test/receipts", unit)
        self.assertIn("/var/lib/orchestra-test/output", unit)
        self.assertNotIn("EnvironmentFile=", unit)


if __name__ == "__main__":
    unittest.main()
