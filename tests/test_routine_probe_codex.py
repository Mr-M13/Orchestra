"""Gated integration test: run the security probe inside Codex's routine sandbox.

This test is intentionally opt-in because it uses a real Codex CLI and provider
session.  It is skipped in normal CI; run it on PixelBox with:

    ORCHESTRA_CODE_INTEGRATION=1 python3 -m unittest \
      tests.test_routine_probe_codex -v

It uses only a temporary worktree and does not touch live services or config.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from omcp import worker_contract
from omcp.runner import run as run_worker


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and os.environ.get("ORCHESTRA_CODE_INTEGRATION") == "1"
    and shutil.which("codex")
    and Path("/etc/orchestra/omcp.json").exists()
    and Path("/etc/orchestra/secrets/deepseek-api-key").exists(),
    "opt-in Linux/Codex integration test with live owner-only config",
)
class CodexRoutineProbeIntegrationTests(unittest.TestCase):
    def test_probe_executes_and_denies_every_assertion(self):
        codex_home = Path.home() / ".codex-ina"
        if not codex_home.is_dir():
            codex_home = Path.home() / ".codex"
        with tempfile.TemporaryDirectory(
            prefix="orchestra-codex-probe-", dir=str(Path.home()),
        ) as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            (workspace / "platform/tests").mkdir(parents=True)
            (workspace / "README.md").write_text("# Probe workspace\n")
            prompt = Path(directory) / "prompt.txt"
            result = Path(directory) / "result.txt"
            completion = Path(directory) / "completion.json"
            spec_path = Path(directory) / "runner.json"
            prompt_text = (
                "This is an isolated sandbox verification. Run the security probe "
                "exactly once, inspect its report, and then write the structured "
                "completion contract. Do not modify any other file.\n\n"
                + worker_contract.contract_instructions(workspace, True)
            )
            prompt.write_text(prompt_text)
            spec_path.write_text(json.dumps({
                "command": ["codex"], "adapter": "codex",
                "codex_home": str(codex_home),
                "workspace": str(workspace),
                "prompt_path": str(prompt), "log_path": str(Path(directory) / "worker.log"),
                "result_path": str(result), "completion_path": str(completion),
                "max_worker_seconds": 300, "max_response_bytes": 16384,
                "require_completion_contract": True,
                "security_probe": True,
                "execution_capabilities": {
                    "profile": "routine_development_v1",
                    "writable_paths": ["platform"],
                    "network": False,
                    "external_credentials": False,
                    "git_metadata": "read_only",
                },
                "git_read_paths": [],
            }))
            self.assertEqual(run_worker(spec_path), 0)
            record = json.loads(completion.read_text())
            self.assertEqual(record["completion_contract"], "success")
            self.assertEqual(record["security_assertions"], "passed")
            checks = {item["name"]: item for item in record["security_checks"]}
            self.assertEqual(set(checks), set(worker_contract.REQUIRED_SECURITY_CHECKS))
            for name, check in checks.items():
                with self.subTest(name=name):
                    self.assertTrue(check["attempted"])
                    self.assertFalse(check["allowed"])
                    self.assertTrue(check["error_type"])


if __name__ == "__main__":
    unittest.main()
