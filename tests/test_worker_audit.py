from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from omcp.controller import Config, Principal, Worker


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_workers", ROOT / "scripts" / "audit_workers.py",
)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class WorkerAuditTests(unittest.TestCase):
    def test_audit_distinguishes_coding_and_response_only_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            credential = base / "key"
            credential.write_text("secret-value-that-must-not-appear\n")
            credential.chmod(0o600)
            config = Config(
                "Mr-M13/Orchestra", root, base / "state", "origin/main",
                {
                    "codex": Worker("codex", ("/bin/true",)),
                    "responder": Worker(
                        "responder", (), adapter="deepseek", provider="deepseek",
                        model="deepseek/deepseek-chat",
                        credential_file=str(credential),
                        endpoint_host="api.deepseek.com",
                    ),
                },
                Principal("chatgpt:test", frozenset({"status:read"}), "test"),
            )
            report = AUDIT.build_audit(config)
        workers = {item["worker"]: item for item in report["workers"]}
        self.assertTrue(workers["codex"]["edits_repository"])
        self.assertFalse(workers["responder"]["edits_repository"])
        self.assertTrue(workers["responder"]["response_only"])
        self.assertEqual(workers["codex"]["funding"], "unverified")
        self.assertTrue(workers["responder"]["credential"]["owner_only"])
        self.assertFalse(report["dispatch_workflow"]["checked"])
        self.assertTrue(report["memory"]["measurement_required"])
        encoded = json.dumps(report)
        self.assertNotIn("secret-value-that-must-not-appear", encoded)
        self.assertNotIn("api-key", encoded.lower())

    def test_audit_requires_a_real_workspace_change_for_proven_health(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            state = base / "state"
            workspace = state / "worktrees" / "default" / "task-1"
            workspace.mkdir(parents=True)
            subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)
            subprocess.run(
                ["git", "-C", str(workspace), "config", "user.email", "audit@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(workspace), "config", "user.name", "Audit"],
                check=True,
            )
            (workspace / "file.txt").write_text("base\n")
            subprocess.run(["git", "-C", str(workspace), "add", "file.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(workspace), "commit", "-q", "-m", "base"],
                check=True,
            )
            (workspace / "file.txt").write_text("changed\n")
            (state / "state.json").write_text(json.dumps({
                "schema_version": 1, "requests": {}, "scheduler": {},
                "tasks": {
                    "11111111-1111-1111-1111-111111111111": {
                        "task_id": "11111111-1111-1111-1111-111111111111",
                        "worker": "codex", "status": "succeeded",
                        "created_at": "2026-10-26T00:00:00+00:00",
                        "finished_at": "2026-10-26T00:01:00+00:00",
                        "workspace": str(workspace),
                        "instructions": "never reported",
                    },
                },
            }))
            config = Config(
                "owner/repo", root, state, "origin/main",
                {"codex": Worker("codex", ("/bin/true",))},
                Principal("chatgpt:test", frozenset({"status:read"}), "test"),
            )
            report = AUDIT.build_audit(config)
        worker = report["workers"][0]
        self.assertTrue(worker["proven_healthy"])
        self.assertEqual(worker["health_reason"], "verified_coding_result")
        self.assertTrue(worker["last_task"]["workspace_changes"])
        self.assertNotIn("never reported", json.dumps(report))

    def test_audit_reports_credit_exhaustion_with_next_action(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            state = base / "state"
            state.mkdir()
            (state / "state.json").write_text(json.dumps({
                "schema_version": 1, "requests": {}, "scheduler": {},
                "tasks": {
                    "11111111-1111-4111-8111-111111111111": {
                        "task_id": "11111111-1111-4111-8111-111111111111",
                        "worker": "ina", "status": "failed", "exit_code": 1,
                        "created_at": "2026-10-26T01:00:00+00:00",
                        "finished_at": "2026-10-26T01:01:00+00:00",
                        "failure": {
                            "type": "CreditExhausted", "category": "credit_exhausted",
                            "stage": "provider", "provider": "openai",
                        },
                    },
                },
            }))
            config = Config(
                "owner/repo", root, state, "origin/main",
                {"ina": Worker("ina", ("/bin/true",))},
                Principal("chatgpt:test", frozenset({"status:read"}), "test"),
            )
            report = AUDIT.build_audit(config)
        worker = report["workers"][0]
        self.assertEqual(worker["failure_category"], "credit_exhausted")
        self.assertEqual(worker["health_reason"], "last_task_credit_exhausted")
        self.assertFalse(worker["proven_healthy"])
        self.assertIn("Add credits to ina", worker["next_action"])
        self.assertEqual(worker["last_task"]["failure_category"], "credit_exhausted")

    def test_audit_does_not_infer_credits_from_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            config = Config(
                "owner/repo", root, base / "state", "origin/main",
                {"codex": Worker("codex", ("/bin/true",))},
                Principal("chatgpt:test", frozenset({"status:read"}), "test"),
                max_parallel_tasks=2,
            )
            report = AUDIT.build_audit(config)
        self.assertEqual(report["workers"][0]["credits"], "unverified")
        self.assertIn("funding/credits are not verified", " ".join(report["warnings"]))
        self.assertIn("available:true", report["note"])


if __name__ == "__main__":
    unittest.main()
