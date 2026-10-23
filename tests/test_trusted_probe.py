"""Trusted, coordinator-controlled routine security verification tests."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from omcp import trusted_probe, worker_contract


def report(allowed_name: str | None = None) -> dict:
    checks = []
    for name in worker_contract.REQUIRED_SECURITY_CHECKS:
        checks.append({
            "name": name, "attempted": True,
            "allowed": name == allowed_name,
            "error_type": "PermissionError", "command": "probe " + name,
        })
    return {"schema_version": 1, "worktree": "/tmp/workspace", "checks": checks}


class TrustedProbeCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.host_config = self.base / "host-omcp.json"
        self.host_secret = self.base / "host-secret"
        self.host_config.write_text("{}")
        self.host_secret.write_text("secret")
        self.task_id = "11111111-1111-1111-1111-111111111111"

    def tearDown(self):
        self.temp.cleanup()

    def test_probe_is_coordinator_owned_and_report_is_trusted(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            probe_root = self.state / "security-probes" / self.task_id
            (probe_root / "orchestra-security-probe.json").write_text(
                json.dumps(report())
            )
            return Mock(returncode=0, stdout="", stderr="")

        with patch("omcp.trusted_probe.shutil.which",
                   side_effect=lambda name: f"/usr/bin/{name}"), \
                patch("omcp.trusted_probe._runtime_dir", return_value=self.base), \
                patch("omcp.trusted_probe.subprocess.run", side_effect=fake_run):
            checks = trusted_probe.run_trusted_security_probe(
                self.workspace, self.state, self.task_id,
                host_config=self.host_config, host_secret=self.host_secret,
            )
        self.assertEqual(set(checks), set(worker_contract.REQUIRED_SECURITY_CHECKS))
        command = calls[0][0]
        probe_root = str(self.state / "security-probes" / self.task_id)
        self.assertIn("--property=ReadWritePaths=" + probe_root, command)
        self.assertIn("--property=ProtectSystem=strict", command)
        self.assertIn("--property=ProtectHome=read-only", command)
        self.assertIn("--property=RestrictSUIDSGID=no", command)
        self.assertIn("--unshare-net", command)
        self.assertIn(probe_root, command)
        self.assertIn("--ro-bind", command)
        self.assertNotIn("--bind=" + str(self.workspace), command)
        self.assertFalse((self.workspace / ".orchestra-tmp").exists())
        self.assertTrue(
            (self.state / "security-probes" / self.task_id / "deny/omcp.json").is_file()
        )
        self.assertEqual(
            (self.state / "security-probes" / self.task_id / "deny/omcp.json").stat().st_mode & 0o777,
            0,
        )

    def test_missing_report_and_allowed_report_fail(self):
        with patch("omcp.trusted_probe.shutil.which",
                   side_effect=lambda name: f"/usr/bin/{name}"), \
                patch("omcp.trusted_probe._runtime_dir", return_value=self.base), \
                patch("omcp.trusted_probe.subprocess.run",
                      return_value=Mock(returncode=0, stdout="", stderr="")):
            with self.assertRaises(trusted_probe.TrustedProbeError):
                trusted_probe.run_trusted_security_probe(
                    self.workspace, self.state, self.task_id,
                    host_config=self.host_config, host_secret=self.host_secret,
                )

        probe_root = self.state / "security-probes" / self.task_id
        (probe_root / "orchestra-security-probe.json").write_text(
            json.dumps(report("read_orchestra_config"))
        )
        with patch("omcp.trusted_probe.shutil.which",
                   side_effect=lambda name: f"/usr/bin/{name}"), \
                patch("omcp.trusted_probe._runtime_dir", return_value=self.base):
            with self.assertRaises(worker_contract.SecurityAssertionError):
                trusted_probe.run_trusted_security_probe(
                    self.workspace, self.state, self.task_id,
                    host_config=self.host_config, host_secret=self.host_secret,
                )


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and os.environ.get("ORCHESTRA_TRUSTED_PROBE_INTEGRATION") == "1"
    and shutil.which("bwrap")
    and shutil.which("systemd-run")
    and Path("/etc/orchestra/omcp.json").exists()
    and Path("/etc/orchestra/secrets/deepseek-api-key").exists(),
    "opt-in Linux trusted-probe integration test",
)
class TrustedProbeIntegrationTests(unittest.TestCase):
    def test_all_negative_assertions_are_attempted_and_denied(self):
        with tempfile.TemporaryDirectory(
            prefix="orchestra-trusted-probe-", dir=str(Path.home()),
        ) as directory:
            base = Path(directory)
            workspace = base / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            (workspace / "platform/tests").mkdir(parents=True)
            state = base / "state"
            state.mkdir()
            task_id = "22222222-2222-2222-2222-222222222222"
            checks = trusted_probe.run_trusted_security_probe(
                workspace, state, task_id,
            )
            self.assertEqual(set(checks), set(worker_contract.REQUIRED_SECURITY_CHECKS))
            for name, check in checks.items():
                with self.subTest(name=name):
                    self.assertTrue(check["attempted"])
                    self.assertFalse(check["allowed"])
                    self.assertTrue(check["error_type"])


if __name__ == "__main__":
    unittest.main()
