"""Tests for structured completion and executed security assertions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from omcp import worker_contract
from omcp.worker_contract import (
    CompletionContractError, SecurityAssertionError,
    read_completion_contract, read_security_report,
)


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = set(worker_contract.REQUIRED_SECURITY_CHECKS)


def _report(checks: list[dict]) -> dict:
    return {"schema_version": 1, "worktree": "/tmp/worktree", "checks": checks}


def _denied(name: str, error_type: str = "PermissionError") -> dict:
    return {
        "name": name, "attempted": True, "allowed": False,
        "error_type": error_type, "command": f"probe {name}",
    }


class CompletionContractTests(unittest.TestCase):
    def test_accepts_success_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "completion.json"
            path.write_text('{"status":"success"}\n')
            self.assertEqual(read_completion_contract(path)["status"], "success")
            path.write_text('{"status":"failure","reason":"required write denied"}\n')
            self.assertEqual(read_completion_contract(path)["status"], "failure")

    def test_rejects_missing_invalid_and_symlink_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "completion.json"
            with self.assertRaises(CompletionContractError):
                read_completion_contract(path)
            path.write_text('{"status":"unknown"}\n')
            with self.assertRaises(CompletionContractError):
                read_completion_contract(path)
            path.write_text('{"status":"success","extra":true}\n')
            with self.assertRaises(CompletionContractError):
                read_completion_contract(path)
            target = root / "target.json"
            target.write_text('{"status":"success"}\n')
            path.unlink()
            path.symlink_to(target)
            with self.assertRaises(CompletionContractError):
                read_completion_contract(path)


class SecurityReportValidationTests(unittest.TestCase):
    def test_accepts_only_all_denied_assertions(self):
        checks = [_denied(name) for name in REQUIRED]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(_report(checks)))
            found = read_security_report(path)
        self.assertEqual(set(found), REQUIRED)
        self.assertTrue(all(value["allowed"] is False for value in found.values()))

    def test_rejects_missing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SecurityAssertionError):
                read_security_report(Path(directory) / "missing.json")

    def test_rejects_allowed_missing_or_unattempted_assertions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            allowed = [_denied(name) for name in REQUIRED]
            allowed[0]["allowed"] = True
            path.write_text(json.dumps(_report(allowed)))
            with self.assertRaisesRegex(SecurityAssertionError, "allowed"):
                read_security_report(path)
            missing = [_denied(name) for name in REQUIRED if name != "network_egress"]
            path.write_text(json.dumps(_report(missing)))
            with self.assertRaisesRegex(SecurityAssertionError, "missing"):
                read_security_report(path)
            unattempted = [_denied(name) for name in REQUIRED]
            unattempted[1]["attempted"] = False
            path.write_text(json.dumps(_report(unattempted)))
            with self.assertRaisesRegex(SecurityAssertionError, "not attempt"):
                read_security_report(path)
            path.write_text('{"schema_version":1,"checks":[]}\n')
            with self.assertRaises(SecurityAssertionError):
                read_security_report(path)


@unittest.skipIf(os.geteuid() == 0, "permission-denial tests require a non-root user")
class ProbeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.workspace = self.base / "repo"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / ".orchestra-contract").mkdir()
        self.config = self.base / "omcp.json"
        self.secret = self.base / "secret"
        self.config.write_text('{"secret":true}\n')
        self.secret.write_text("secret-value\n")
        self.config.chmod(0)
        self.secret.chmod(0)
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.outside.chmod(0o500)
        self.report = self.workspace / ".orchestra-contract/security.json"
        self.addCleanup(self._restore_permissions)

    def _restore_permissions(self):
        self.config.chmod(0o600)
        self.secret.chmod(0o600)
        self.outside.chmod(0o700)
        git_dir = self.workspace / ".git"
        if git_dir.exists():
            git_dir.chmod(0o700)
        self.temp.cleanup()

    def _run_probe(self, *, config: Path | None = None):
        (self.workspace / ".git").chmod(0o500)
        completed = subprocess.run(
            [
                sys.executable, "-m", "omcp.routine_probe",
                "--worktree", str(self.workspace),
                "--report", str(self.report),
                "--orchestra-config", str(config or self.config),
                "--orchestra-secret", str(self.secret),
                "--outside-target", str(self.outside / "probe.txt"),
                "--network-host", "127.0.0.1",
                "--network-port", "1",
            ],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(self.report.read_text())

    def test_probe_attempts_every_assertion_and_records_denial(self):
        report = self._run_probe()
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(set(checks), REQUIRED)
        for name, check in checks.items():
            with self.subTest(name=name):
                self.assertTrue(check["attempted"])
                self.assertFalse(check["allowed"])
                self.assertTrue(check["error_type"])
        found = read_security_report(self.report)
        self.assertEqual(set(found), REQUIRED)

    def test_probe_allowed_read_is_rejected_by_contract_validator(self):
        self.config.chmod(0o600)
        report = self._run_probe()
        self.assertTrue(any(
            check["name"] == "read_orchestra_config" and check["allowed"]
            for check in report["checks"]
        ))
        with self.assertRaisesRegex(SecurityAssertionError, "allowed"):
            read_security_report(self.report)


if __name__ == "__main__":
    unittest.main()
