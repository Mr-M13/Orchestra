from __future__ import annotations

import importlib.util
import json
import os
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
