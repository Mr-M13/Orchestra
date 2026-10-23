"""Trusted, coordinator-controlled routine security verification tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from omcp import trusted_probe, worker_contract
from omcp.routine_policy import RoutineConfinementPolicy


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
        self.task_id = "11111111-1111-1111-1111-111111111111"

    def tearDown(self):
        self.temp.cleanup()

    def test_probe_uses_shared_codex_policy_and_coordinator_state(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return Mock(returncode=0, stdout=json.dumps(report()) + "\n", stderr="")

        with patch("omcp.trusted_probe.shutil.which",
                   side_effect=lambda name: f"/usr/bin/{name}"), \
                patch("omcp.trusted_probe._runtime_dir", return_value=self.base), \
                patch("omcp.trusted_probe.subprocess.run", side_effect=fake_run):
            checks = trusted_probe.run_trusted_security_probe(
                self.workspace, self.state, self.task_id, ("platform",), (),
            )
        self.assertEqual(set(checks), set(worker_contract.REQUIRED_SECURITY_CHECKS))
        command = calls[0][0]
        probe_root = self.state / "security-probes" / self.task_id
        self.assertIn("/usr/bin/codex", command)
        self.assertIn("sandbox", command)
        self.assertIn("-P", command)
        self.assertIn("orchestra-routine", command)
        self.assertIn("--property=ProtectSystem=strict", command)
        self.assertIn("--property=ProtectHome=read-only", command)
        self.assertIn("--property=RestrictSUIDSGID=no", command)
        self.assertIn("--property=ReadWritePaths=" + str(probe_root / "codex-home"), command)
        self.assertIn("--property=ReadWritePaths=" + str(self.workspace), command)
        self.assertIn("permissions.orchestra-routine.network.enabled=false", command)
        self.assertIn("permissions.orchestra-routine.filesystem./etc/orchestra=\"deny\"", command)
        self.assertIn("--report", command)
        self.assertIn("-", command)
        self.assertFalse((self.workspace / ".orchestra-tmp").exists())
        self.assertTrue((probe_root / "orchestra-security-probe.py").is_file())
        self.assertTrue((probe_root / "orchestra-security-probe.json").is_file())

    def test_missing_or_allowed_report_fails(self):
        with patch("omcp.trusted_probe.shutil.which",
                   side_effect=lambda name: f"/usr/bin/{name}"), \
                patch("omcp.trusted_probe._runtime_dir", return_value=self.base), \
                patch("omcp.trusted_probe.subprocess.run",
                      return_value=Mock(returncode=0, stdout="", stderr="")):
            with self.assertRaises(trusted_probe.TrustedProbeError):
                trusted_probe.run_trusted_security_probe(
                    self.workspace, self.state, self.task_id, ("platform",), (),
                )

        with patch("omcp.trusted_probe.shutil.which",
                   side_effect=lambda name: f"/usr/bin/{name}"), \
                patch("omcp.trusted_probe._runtime_dir", return_value=self.base), \
                patch("omcp.trusted_probe.subprocess.run",
                      return_value=Mock(
                          returncode=0,
                          stdout=json.dumps(report("read_orchestra_config")) + "\n",
                          stderr="",
                      )):
            with self.assertRaises(worker_contract.SecurityAssertionError):
                trusted_probe.run_trusted_security_probe(
                    self.workspace, self.state, self.task_id, ("platform",), (),
                )

    def test_worker_and_probe_share_the_same_policy_arguments(self):
        policy = RoutineConfinementPolicy(
            ("platform", "theme"), ("/tmp/owner-checkout",),
            network_enabled=False, deny_orchestra_config=True,
        )
        worker = policy.worker_command("/usr/bin/codex", self.workspace, self.base / "result")
        probe = policy.probe_command("/usr/bin/codex", self.workspace, ["/usr/bin/python3"])
        for token in (
            "permissions.orchestra-routine.network.enabled=false",
            "permissions.orchestra-routine.filesystem./etc/orchestra=\"deny\"",
            "permissions.orchestra-routine.filesystem.:workspace_roots=",
        ):
            self.assertTrue(any(token in item for item in worker))
            self.assertTrue(any(token in item for item in probe))
        self.assertIn("sandbox", probe)
        self.assertIn("-P", probe)

    def test_weakening_shared_policy_weakens_both_paths(self):
        strong = RoutineConfinementPolicy(("platform",))
        weak = RoutineConfinementPolicy(
            ("platform",), network_enabled=True, deny_orchestra_config=False,
            deny_tmpdir=False, deny_slash_tmp=False,
        )
        strong_worker = strong.worker_command("/usr/bin/codex", self.workspace, self.base / "result")
        strong_probe = strong.probe_command("/usr/bin/codex", self.workspace, ["/usr/bin/python3"])
        weak_worker = weak.worker_command("/usr/bin/codex", self.workspace, self.base / "result")
        weak_probe = weak.probe_command("/usr/bin/codex", self.workspace, ["/usr/bin/python3"])
        self.assertIn("permissions.orchestra-routine.network.enabled=false", strong_worker)
        self.assertIn("permissions.orchestra-routine.network.enabled=false", strong_probe)
        self.assertIn("permissions.orchestra-routine.network.enabled=true", weak_worker)
        self.assertIn("permissions.orchestra-routine.network.enabled=true", weak_probe)
        self.assertIn("permissions.orchestra-routine.filesystem./etc/orchestra=\"deny\"", strong_worker)
        self.assertNotIn("permissions.orchestra-routine.filesystem./etc/orchestra=\"deny\"", weak_worker)
        self.assertNotIn("permissions.orchestra-routine.filesystem./etc/orchestra=\"deny\"", weak_probe)
        for token in (
            "permissions.orchestra-routine.filesystem.:tmpdir=\"deny\"",
            "permissions.orchestra-routine.filesystem.:slash_tmp=\"deny\"",
        ):
            self.assertIn(token, strong_worker)
            self.assertIn(token, strong_probe)
            self.assertNotIn(token, weak_worker)
            self.assertNotIn(token, weak_probe)


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and os.environ.get("ORCHESTRA_TRUSTED_PROBE_INTEGRATION") == "1"
    and shutil.which("bwrap")
    and shutil.which("systemd-run")
    and shutil.which("codex")
    and Path("/etc/orchestra/omcp.json").exists()
    and Path("/etc/orchestra/secrets/deepseek-api-key").exists(),
    "opt-in Linux trusted-probe integration test",
)
class TrustedProbeIntegrationTests(unittest.TestCase):
    def _workspace(self, base: Path) -> Path:
        workspace = base / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        (workspace / "platform/tests").mkdir(parents=True)
        return workspace

    def test_all_negative_assertions_are_attempted_and_denied(self):
        with tempfile.TemporaryDirectory(
            prefix="orchestra-trusted-probe-", dir=str(Path.home()),
        ) as directory:
            base = Path(directory)
            workspace = self._workspace(base)
            state = base / "state"
            state.mkdir()
            checks = trusted_probe.run_trusted_security_probe(
                workspace, state, "22222222-2222-2222-2222-222222222222",
                ("platform",), (),
            )
            self.assertEqual(set(checks), set(worker_contract.REQUIRED_SECURITY_CHECKS))
            for name, check in checks.items():
                with self.subTest(name=name):
                    self.assertTrue(check["attempted"])
                    self.assertFalse(check["allowed"])
                    self.assertTrue(check["error_type"])

    def test_weakening_the_shared_policy_makes_the_probe_detect_an_allowed_read(self):
        with tempfile.TemporaryDirectory(
            prefix="orchestra-trusted-probe-weak-", dir=str(Path.home()),
        ) as directory:
            base = Path(directory)
            workspace = self._workspace(base)
            state = base / "state"
            state.mkdir()
            task_id = "33333333-3333-3333-3333-333333333333"
            probe_root, _, _ = trusted_probe.prepare_probe_root(state, task_id)
            policy = RoutineConfinementPolicy(
                ("platform",), (str(probe_root),),
                deny_orchestra_config=False,
            )
            with self.assertRaises(worker_contract.SecurityAssertionError):
                trusted_probe.run_trusted_security_probe(
                    workspace, state, task_id, ("platform",), (str(probe_root),),
                    policy=policy,
                )


if __name__ == "__main__":
    unittest.main()
