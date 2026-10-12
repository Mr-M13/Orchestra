import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from omcp.approval import format_review
from omcp.controller import (
    AuthorizationError, Config, Principal, RepositoryProfile, StandingAuthorization,
    TaskController, Worker,
)
from omcp.local_control import LocalApprovalServer, approval_request
from omcp.runner import run as run_worker
from omcp.server import MCPServer, TOOLS, main as server_main


ALL_PERMISSIONS = frozenset({
    "status:read", "tasks:submit", "tasks:read", "results:read",
    "workers:read", "tasks:cancel", "approvals:read",
})


class FakeProcess:
    pid = os.getpid()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.state = Path(self.temp.name) / "state"
        self.config = Config(
            "owner/repo", self.root, self.state, "origin/main",
            {"ina": Worker("ina", ("codex",))},
            Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
        )
        self.popen = Mock(return_value=FakeProcess())
        self.controller = TaskController(
            self.config, popen=self.popen,
            workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def submit(self, request_id="request-1", approval=True):
        submitted = self.controller.submit_task(
            request_id, "Implement feature", "Make the requested change", "ina", approval,
        )
        if not submitted.get("idempotent_replay"):
            approved = self.controller.resolve_approval(
                submitted["task_id"], "approve", "Test operator approved",
                approver_identity="local:test-operator",
            )
            return {**submitted, **approved}
        return submitted

    def complete(self, task_id, exit_code=0, result="finished"):
        task = self.controller.state["tasks"][task_id]
        Path(task["result_path"]).write_text(result)
        Path(task["completion_path"]).write_text(json.dumps({
            "exit_code": exit_code, "finished_at": "2026-10-11T00:00:00+00:00",
            "runner_error": None,
        }))

    def test_submission_is_durable_acknowledged_and_idempotent(self):
        first = self.submit()
        self.assertTrue(first["acknowledged"])
        self.assertEqual(first["status"], "running")
        replay = self.submit()
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["task_id"], first["task_id"])
        self.assertEqual(len(self.controller.state["tasks"]), 1)
        persisted = json.loads((self.state / "state.json").read_text())
        self.assertEqual(persisted["requests"]["request-1"], first["task_id"])
        with self.assertRaisesRegex(ValueError, "different task content"):
            self.controller.submit_task("request-1", "Other", "Other work", "ina")

    def test_profile_is_bound_to_task_approval_and_idempotency(self):
        archive = Path(self.temp.name) / "archive"
        archive.mkdir()
        profiles = {
            "orchestra": RepositoryProfile("orchestra", "owner/repo", self.root, "origin/main"),
            "archive": RepositoryProfile("archive", "owner/archive", archive, "origin/release"),
        }
        self.controller.close()
        self.controller = TaskController(
            Config("owner/repo", self.root, self.state, "origin/main",
                   {"ina": Worker("ina", ("codex",))},
                   Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                   repository_profiles=profiles, default_profile="orchestra"),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )
        submitted = self.controller.submit_task(
            "profile-request", "Inspect", "Read only", "ina", True, "archive",
        )
        self.assertEqual(submitted["repository_profile"], "archive")
        self.assertEqual(submitted["repository"], "owner/archive")
        details = self.controller.local_approval_details(submitted["task_id"])
        self.assertEqual(details["repository_profile"], "archive")
        with self.assertRaisesRegex(ValueError, "different task content"):
            self.controller.submit_task("profile-request", "Inspect", "Read only", "ina", True, "orchestra")

    def test_multi_repository_requires_explicit_profile_before_persistence(self):
        archive = Path(self.temp.name) / "archive"
        archive.mkdir()
        self.controller.close()
        self.controller = TaskController(
            Config("owner/repo", self.root, self.state, "origin/main",
                   {"ina": Worker("ina", ("codex",))},
                   Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                   repository_profiles={
                       "orchestra": RepositoryProfile("orchestra", "owner/repo", self.root, "origin/main"),
                       "archive": RepositoryProfile("archive", "owner/archive", archive, "origin/main"),
                   },
                   default_profile="orchestra"),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )
        with self.assertRaisesRegex(ValueError, "repository_profile is required"):
            self.controller.submit_task("missing-profile", "Inspect", "Read only", "ina")
        self.assertEqual(self.controller.state["tasks"], {})
        self.popen.assert_not_called()
        submitted = self.controller.submit_task(
            "explicit-profile", "Inspect", "Read only", "ina", True, "archive",
        )
        self.assertEqual(submitted["repository_profile"], "archive")
        self.assertEqual(submitted["repository"], "owner/archive")

    def test_multi_repository_bridge_status_uses_default_profile(self):
        archive = Path(self.temp.name) / "archive"
        archive.mkdir()
        self.controller.close()
        self.controller = TaskController(
            Config("owner/repo", self.root, self.state, "origin/main",
                   {"ina": Worker("ina", ("codex",))},
                   Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                   repository_profiles={
                       "orchestra": RepositoryProfile("orchestra", "owner/repo", self.root, "origin/main"),
                       "archive": RepositoryProfile("archive", "owner/archive", archive, "origin/main"),
                   },
                   default_profile="orchestra"),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )

        status = self.controller.bridge_status()

        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["repository"], "owner/repo")
        self.assertEqual(status["default_profile"], "orchestra")
        self.assertEqual(status["repository_profiles"], ["archive", "orchestra"])
        self.assertEqual(self.controller.state["tasks"], {})
        self.popen.assert_not_called()

    def test_bounded_routine_development_uses_standing_authorization(self):
        self.controller.close()
        self.controller = TaskController(
            Config(
                "owner/repo", self.root, self.state, "origin/main",
                {"ina": Worker("ina", ("codex",))},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                standing_authorizations=(StandingAuthorization(
                    "routine-local-development", "routine_development",
                    frozenset({"default"}), frozenset({"ina"}), ("src", "tests"),
                ),),
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )

        submitted = self.controller.submit_task(
            "routine-1", "Implement tests", "Change local code and run tests", "ina",
            True, "default", "routine_development",
        )

        self.assertEqual(submitted["status"], "running")
        self.assertEqual(submitted["consequence_class"], "routine_development")
        self.assertEqual(submitted["authorization"], {
            "mode": "standing", "authorization_id": "routine-local-development",
        })
        self.assertEqual(submitted["approval"]["status"], "standing_authorized")
        self.assertEqual(submitted["execution_capabilities"], {
            "profile": "routine_development_v1", "writable_paths": ["src", "tests"],
            "network": False, "external_credentials": False, "git_metadata": "read_only",
        })
        self.assertEqual(self.controller.list_approvals()["approvals"], [])
        self.popen.assert_called_once()
        audit = (self.state / "audit.jsonl").read_text()
        self.assertIn('"authorization_mode":"standing"', audit)
        self.assertNotIn("Change local code and run tests", audit)

    def test_protected_and_unclassified_work_still_requires_explicit_approval(self):
        self.controller.close()
        ids = iter([
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        ])
        self.controller = TaskController(
            Config(
                "owner/repo", self.root, self.state, "origin/main",
                {"ina": Worker("ina", ("codex",))},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                standing_authorizations=(StandingAuthorization(
                    "routine-local-development", "routine_development",
                    frozenset({"default"}), frozenset({"ina"}), (),
                ),),
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: next(ids),
        )

        for index, consequence_class in enumerate((
            "production_deployment", "destructive_operation", "security_change", "unclassified",
        )):
            submitted = self.controller.submit_task(
                f"protected-{index}", "Consequential work", "Do consequential work", "ina",
                True, "default", consequence_class,
            )
            self.assertEqual(submitted["status"], "approval_required")
            self.assertEqual(submitted["authorization"], {"mode": "explicit"})
            self.assertEqual(submitted["approval"]["status"], "pending")
        self.assertEqual(len(self.controller.list_approvals()["approvals"]), 4)
        self.popen.assert_not_called()

    def test_routine_work_outside_granted_worker_scope_requires_explicit_approval(self):
        self.controller.close()
        self.controller = TaskController(
            Config(
                "owner/repo", self.root, self.state, "origin/main",
                {
                    "ina": Worker("ina", ("codex",)),
                    "codex": Worker("codex", ("codex",)),
                },
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                standing_authorizations=(StandingAuthorization(
                    "ina-routine-only", "routine_development",
                    frozenset({"default"}), frozenset({"ina"}), (),
                ),),
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )

        submitted = self.controller.submit_task(
            "wrong-worker", "Routine work", "Change local code", "codex",
            True, "default", "routine_development",
        )

        self.assertEqual(submitted["status"], "approval_required")
        self.assertEqual(submitted["authorization"], {"mode": "explicit"})
        self.popen.assert_not_called()

    def test_consequence_class_is_bound_to_idempotency(self):
        first = self.controller.submit_task(
            "classified", "Implement", "Change code", "ina", True, None,
            "routine_development",
        )
        self.assertEqual(first["status"], "approval_required")
        with self.assertRaisesRegex(ValueError, "different task content"):
            self.controller.submit_task(
                "classified", "Implement", "Change code", "ina", True, None,
                "security_change",
            )

    def test_unknown_profile_fails_before_task_is_persisted(self):
        with self.assertRaisesRegex(ValueError, "unknown repository profile"):
            self.controller.submit_task("unknown-profile", "Inspect", "Read only", "ina", True, "missing")
        self.assertEqual(self.controller.state["tasks"], {})

    def test_completion_status_result_and_availability(self):
        submitted = self.submit()
        self.assertFalse(self.controller.worker_availability()["workers"][0]["available"])
        self.complete(submitted["task_id"], result="verified result")
        status = self.controller.task_status(submitted["task_id"])
        self.assertEqual(status["status"], "succeeded")
        result = self.controller.task_result(submitted["task_id"])
        self.assertEqual(result["result"], "verified result")
        self.assertFalse(result["truncated"])
        self.assertTrue(self.controller.worker_availability()["workers"][0]["available"])

    def test_approval_gate_can_reject_without_launching(self):
        submitted = self.controller.submit_task(
            "request-1", "Implement feature", "Make the requested change", "ina", True,
        )
        self.assertEqual(submitted["status"], "approval_required")
        self.assertEqual(len(self.controller.list_approvals()["approvals"]), 1)
        result = self.controller.resolve_approval(
            submitted["task_id"], "reject", "Not authorised", approver_identity="local:test-operator",
        )
        self.assertEqual(result["status"], "rejected")
        self.popen.assert_not_called()

    def test_approval_gate_launches_only_after_approval(self):
        submitted = self.controller.submit_task(
            "request-1", "Implement feature", "Make the requested change", "ina", True,
        )
        result = self.controller.resolve_approval(
            submitted["task_id"], "approve", "Scope reviewed", approver_identity="local:test-operator",
        )
        self.assertEqual(result["status"], "running")
        self.popen.assert_called_once()

    def test_expired_approval_is_rejected_and_cannot_launch(self):
        submitted = self.controller.submit_task(
            "request-1", "Implement feature", "Make the requested change", "ina", True,
        )
        task = self.controller.state["tasks"][submitted["task_id"]]
        task["approval"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "expired"):
            self.controller.resolve_approval(
                submitted["task_id"], "approve", "Too late",
                approver_identity="local:test-operator",
            )
        self.assertEqual(task["status"], "rejected")
        self.assertEqual(task["approval"]["status"], "expired")
        self.popen.assert_not_called()
        audit = (self.state / "audit.jsonl").read_text()
        self.assertIn('"action":"approval.expire"', audit)

    def test_invalid_approval_expiry_fails_closed(self):
        submitted = self.controller.submit_task(
            "request-1", "Implement feature", "Make the requested change", "ina", True,
        )
        task = self.controller.state["tasks"][submitted["task_id"]]
        task["approval"].pop("expires_at")
        with self.assertRaisesRegex(ValueError, "expired"):
            self.controller.local_approval_details(submitted["task_id"])
        self.assertEqual(task["status"], "rejected")
        self.popen.assert_not_called()

    def test_local_approval_review_contains_operation_and_arguments(self):
        submitted = self.controller.submit_task(
            "request-1", "Implement feature", "Make the requested change", "ina", True,
        )
        details = self.controller.local_approval_details(submitted["task_id"])
        review = format_review(details, "approve")
        self.assertIn(submitted["task_id"], review)
        self.assertIn("request-1", review)
        self.assertIn("Implement feature", review)
        self.assertIn("Make the requested change", review)
        self.assertIn("Worker:       ina", review)
        self.assertIn("Decision:     APPROVE", review)

    def test_local_details_and_history_include_terminal_approval_state(self):
        submitted = self.controller.submit_task(
            "request-1", "Inspect", "Read the implementation", "ina", True,
        )
        self.controller.resolve_approval(
            submitted["task_id"], "reject", "Out of scope", "local:test-operator",
        )
        details = self.controller.local_task_details(submitted["task_id"])
        self.assertEqual(details["instructions"], "Read the implementation")
        self.assertEqual(details["status"], "rejected")
        self.assertEqual(details["approval"]["status"], "rejected")
        history = self.controller.local_approval_history()
        self.assertEqual(history["approvals"][0]["task_id"], submitted["task_id"])
        self.assertNotIn("instructions", history["approvals"][0])

    def test_pending_task_can_be_cancelled_with_reason(self):
        submitted = self.controller.submit_task(
            "request-1", "Implement feature", "Make the requested change", "ina", True,
        )
        result = self.controller.cancel_task(submitted["task_id"], "No longer needed")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["cancel_reason"], "No longer needed")

    def test_list_tasks_filters_and_bounds_results(self):
        submitted = self.controller.submit_task(
            "request-1", "Implement feature", "Make the requested change", "ina", True,
        )
        found = self.controller.list_tasks("approval_required", 1)
        self.assertEqual([submitted["task_id"]], [item["task_id"] for item in found["tasks"]])
        with self.assertRaisesRegex(ValueError, "limit"):
            self.controller.list_tasks(limit=0)

    def test_audit_log_records_identity_but_not_task_instructions(self):
        self.submit(approval=True)
        audit = (self.state / "audit.jsonl").read_text()
        self.assertIn('"principal":"chatgpt:test"', audit)
        self.assertIn('"action":"task.submit"', audit)
        self.assertNotIn("Make the requested change", audit)

    def test_permission_denial_fails_closed_and_is_audited(self):
        self.controller.close()
        limited_state = Path(self.temp.name) / "limited-state"
        config = Config(
            "owner/repo", self.root, limited_state, "origin/main",
            {"ina": Worker("ina", ("codex",))},
            Principal("readonly", frozenset({"status:read"}), "test-session"),
        )
        self.controller = TaskController(config, workspace_preparer=lambda *_: self.root)
        with self.assertRaises(AuthorizationError):
            self.controller.submit_task("request-1", "Title", "Instructions", "ina")
        self.assertIn('"outcome":"denied"', (limited_state / "audit.jsonl").read_text())

    def test_result_is_bounded(self):
        self.controller.config = Config(
            self.config.repository, self.config.project_root, self.config.state_dir,
            self.config.base_ref, self.config.workers, self.config.principal,
            max_result_bytes=1024,
        )
        submitted = self.submit()
        self.complete(submitted["task_id"], result="x" * 2000)
        result = self.controller.task_result(submitted["task_id"])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["result"]), 1024)


class ConfigTests(unittest.TestCase):
    def test_config_loads_bounded_routine_standing_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            codex = Path(directory) / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o700)
            config_path = Path(directory) / "omcp.json"
            config_path.write_text(json.dumps({
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(Path(directory) / "state"),
                "workers": {"ina": {"command": [str(codex)]}},
                "principal": {"identity": "chatgpt:test", "permissions": ["tasks:submit"]},
                "standing_authorizations": [{
                    "id": "routine", "consequence_class": "routine_development",
                    "repository_profiles": ["default"], "workers": ["ina"],
                    "writable_paths": ["src", "tests"],
                }],
            }))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                config = Config.load(config_path)
            self.assertEqual(config.standing_authorizations, (StandingAuthorization(
                "routine", "routine_development", frozenset({"default"}),
                frozenset({"ina"}), ("src", "tests"),
            ),))

    def test_config_rejects_routine_write_access_to_control_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            codex = Path(directory) / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o700)
            config_path = Path(directory) / "omcp.json"
            raw = {
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(Path(directory) / "state"),
                "workers": {"ina": {"command": [str(codex)]}},
                "principal": {"identity": "chatgpt:test", "permissions": ["tasks:submit"]},
                "standing_authorizations": [{
                    "id": "routine", "consequence_class": "routine_development",
                    "repository_profiles": ["default"], "workers": ["ina"],
                    "writable_paths": [".git/hooks"],
                }],
            }
            config_path.write_text(json.dumps(raw))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                with self.assertRaisesRegex(ValueError, "safe relative paths"):
                    Config.load(config_path)

    def test_config_requires_owner_only_file_and_rejects_cli_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            config_path = Path(directory) / "omcp.json"
            raw = {
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(Path(directory) / "state"),
                "workers": {"ina": {"command": ["codex", "--sandbox", "danger-full-access"]}},
                "principal": {"identity": "chatgpt", "permissions": ["status:read"]},
            }
            config_path.write_text(json.dumps(raw))
            os.chmod(config_path, 0o600)
            with self.assertRaisesRegex(ValueError, "may not contain CLI arguments"):
                Config.load(config_path)
            canonical = Path(directory) / "canonical-codex"
            other = Path(directory) / "other-codex"
            canonical.write_text("#!/bin/sh\n")
            other.write_text("#!/bin/sh\n")
            canonical.chmod(0o755)
            other.chmod(0o755)
            raw["workers"]["ina"]["command"] = [str(other)]
            config_path.write_text(json.dumps(raw))
            with patch("omcp.controller.shutil.which", return_value=str(canonical)):
                with self.assertRaisesRegex(ValueError, "canonical Codex executable"):
                    Config.load(config_path)
            os.chmod(config_path, 0o644)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                Config.load(config_path)

    def test_submit_cannot_disable_human_approval(self):
        controller = object.__new__(TaskController)
        controller.config = Config(
            "owner/repo", Path("/tmp"), Path("/tmp/state"), "origin/main",
            {"worker": Worker("worker", ("codex",))},
            Principal("chatgpt:test", frozenset({"tasks:submit"}), "test-session"),
        )
        controller._authorize = Mock()
        with self.assertRaisesRegex(ValueError, "mandatory"):
            controller.submit_task("r", "title", "instructions", "worker", False)

    def test_config_rejects_standing_authorization_for_protected_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            codex = Path(directory) / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o700)
            config_path = Path(directory) / "omcp.json"
            config_path.write_text(json.dumps({
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(Path(directory) / "state"),
                "workers": {"ina": {"command": [str(codex)]}},
                "principal": {"identity": "chatgpt:test", "permissions": ["tasks:submit"]},
                "standing_authorizations": [{
                    "id": "unsafe", "consequence_class": "security_change",
                    "repository_profiles": ["default"], "workers": ["ina"],
                }],
            }))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                with self.assertRaisesRegex(ValueError, "only grant routine_development"):
                    Config.load(config_path)

    def test_submitter_cannot_self_approve(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            state = Path(directory) / "state"
            config = Config(
                "owner/repo", root, state, "origin/main",
                {"ina": Worker("ina", ("codex",))},
                Principal("local:submitter", ALL_PERMISSIONS, "test-session"),
            )
            controller = TaskController(config, workspace_preparer=lambda *_: root)
            try:
                submitted = controller.submit_task(
                    "request-1", "Title", "Instructions", "ina", True,
                )
                with self.assertRaisesRegex(AuthorizationError, "cannot approve"):
                    controller.resolve_approval(
                        submitted["task_id"], "approve", "self-approval",
                        approver_identity="local:submitter",
                    )
                self.assertEqual(
                    controller.task_status(submitted["task_id"])["status"], "approval_required",
                )
                audit = (state / "audit.jsonl").read_text()
                self.assertIn('"outcome":"denied"', audit)
                self.assertIn("submitter_cannot_approve", audit)
            finally:
                controller.close()


class RunnerTests(unittest.TestCase):
    def test_runner_passes_only_explicitly_permitted_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            codex = bin_dir / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o700)
            prompt = root / "prompt"
            prompt.write_text("task")
            spec = root / "spec.json"
            spec.write_text(json.dumps({
                "command": ["codex"], "workspace": str(root),
                "prompt_path": str(prompt), "log_path": str(root / "log"),
                "result_path": str(root / "result"),
                "completion_path": str(root / "completion"),
                "codex_home": "/private/codex-home",
            }))
            parent_environment = {
                "PATH": str(bin_dir), "HOME": "/Users/test",
                "TMPDIR": "/tmp/test", "LANG": "en_AU.UTF-8",
                "LC_ALL": "en_AU.UTF-8", "CODEX_HOME": "/parent/codex-home",
                "CONTROL_PLANE_API_KEY": "tunnel-secret",
                "OMCP_AUTHENTICATED_CALLER": "chatgpt:principal",
                "OMCP_TUNNEL_AUTHENTICATED": "1", "OPENAI_API_KEY": "api-secret",
                "HTTPS_PROXY": "https://proxy.example", "GIT_TERMINAL_PROMPT": "1",
            }
            with patch.dict(os.environ, parent_environment, clear=True):
                with patch("omcp.runner.subprocess.run", return_value=Mock(returncode=0)) as execute:
                    self.assertEqual(run_worker(spec), 0)

            environment = execute.call_args.kwargs["env"]
            self.assertEqual(environment, {
                "PATH": str(bin_dir), "HOME": "/Users/test",
                "TMPDIR": "/tmp/test", "LANG": "en_AU.UTF-8",
                "LC_ALL": "en_AU.UTF-8", "CODEX_HOME": "/private/codex-home",
                "GIT_TERMINAL_PROMPT": "0",
            })

    def test_runner_uses_codex_home_from_environment_when_not_configured(self):
        from omcp.runner import worker_environment
        with patch.dict(os.environ, {"PATH": "/trusted/bin", "CODEX_HOME": "/parent/codex-home",
                                     "CONTROL_PLANE_API_KEY": "tunnel-secret"}, clear=True):
            self.assertEqual(worker_environment(None), {
                "PATH": "/trusted/bin", "CODEX_HOME": "/parent/codex-home",
                "GIT_TERMINAL_PROMPT": "0",
            })

    def test_runner_writes_completion_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt"
            prompt.write_text("task")
            spec = root / "spec.json"
            completion = root / "completion.json"
            spec.write_text(json.dumps({
                "command": ["codex"], "workspace": str(root),
                "prompt_path": str(prompt), "log_path": str(root / "log"),
                "result_path": str(root / "result"), "completion_path": str(completion),
                "codex_home": None,
            }))
            with patch("omcp.runner.subprocess.run", return_value=Mock(returncode=0)) as execute:
                self.assertEqual(run_worker(spec), 0)
            self.assertEqual(json.loads(completion.read_text())["exit_code"], 0)
            command = execute.call_args.args[0]
            self.assertEqual(command.count("--sandbox"), 1)
            self.assertIn("--ask-for-approval", command)
            self.assertEqual(command[command.index("--ask-for-approval") + 1], "never")
            self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
            self.assertEqual(command[-1], "-")

    def test_standing_runner_uses_restricted_permission_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt"
            prompt.write_text("task")
            spec = root / "spec.json"
            spec.write_text(json.dumps({
                "command": ["codex"], "workspace": str(root),
                "prompt_path": str(prompt), "log_path": str(root / "log"),
                "result_path": str(root / "result"),
                "completion_path": str(root / "completion"), "codex_home": None,
                "execution_capabilities": {
                    "profile": "routine_development_v1", "writable_paths": ["src"],
                    "network": False, "external_credentials": False,
                    "git_metadata": "read_only",
                },
            }))
            with patch("omcp.runner.subprocess.run", return_value=Mock(returncode=0)) as execute:
                self.assertEqual(run_worker(spec), 0)
            command = execute.call_args.args[0]
            self.assertNotIn("--sandbox", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--ignore-rules", command)
            self.assertIn("--ephemeral", command)
            self.assertIn("permissions.orchestra-routine.network.enabled=false", command)
            self.assertIn(
                'permissions.orchestra-routine.filesystem.:workspace_roots={"."="read",".orchestra-tmp"="write","src"="write"}',
                command,
            )
            self.assertIn('".orchestra-tmp"="write"', " ".join(command))
            self.assertFalse((root / ".orchestra-tmp").exists())
            self.assertEqual(execute.call_args.kwargs["env"]["TMPDIR"], str(root / ".orchestra-tmp"))
    def test_runner_rejects_configured_cli_flags_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt"
            prompt.write_text("task")
            spec = root / "spec.json"
            spec.write_text(json.dumps({
                "command": ["codex", "--sandbox", "danger-full-access"],
                "workspace": str(root), "prompt_path": str(prompt),
                "log_path": str(root / "log"), "result_path": str(root / "result"),
                "completion_path": str(root / "completion"), "codex_home": None,
            }))
            with patch("omcp.runner.subprocess.run") as execute:
                with self.assertRaisesRegex(ValueError, "exactly one Codex executable"):
                    run_worker(spec)
                execute.assert_not_called()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.controller = Mock()
        self.controller.bridge_status.return_value = {"status": "ok"}
        self.server = MCPServer(self.controller)

    def test_initialize_and_tool_discovery(self):
        initialized = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "orchestra-omcp")
        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertEqual({tool["name"] for tool in TOOLS},
                         {tool["name"] for tool in listed["result"]["tools"]})

    def test_tool_call_returns_structured_content(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "bridge_status", "arguments": {}},
        })
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"], {"status": "ok"})

    def test_mutating_server_startup_requires_authenticated_tunnel_identity(self):
        config = Mock()
        with patch("omcp.server.Config.load", return_value=config), \
             patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(PermissionError, "authenticated tunnel"):
                server_main(["--config", "/unused/config.json"])

    def test_mutating_server_startup_binds_exact_configured_caller(self):
        config = Mock()
        config.principal.identity = "chatgpt:expected"
        env = {
            "OMCP_TUNNEL_AUTHENTICATED": "1",
            "OMCP_AUTHENTICATED_CALLER": "chatgpt:other",
        }
        with patch("omcp.server.Config.load", return_value=config), \
             patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(PermissionError, "does not match"):
                server_main(["--config", "/unused/config.json"])

    def test_unknown_method_uses_json_rpc_error(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 4, "method": "unknown"})
        self.assertEqual(response["error"]["code"], -32601)


class LocalApprovalChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.config = Config(
            "owner/repo", self.root, Path(self.temp.name) / "state", "origin/main",
            {"ina": Worker("ina", ("codex",))},
            Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
        )
        self.popen = Mock(return_value=FakeProcess())
        self.controller = TaskController(
            self.config, popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )
        self.channel = LocalApprovalServer(self.controller, threading.RLock())
        self.channel.start()

    def tearDown(self):
        self.channel.stop()
        self.controller.close()
        self.temp.cleanup()

    def test_review_and_resolution_use_active_coordinator(self):
        submitted = self.controller.submit_task(
            "request-1", "Review me", "Read only", "ina", True,
        )
        details = approval_request(self.config.state_dir, {
            "action": "review", "task_id": submitted["task_id"],
        })
        self.assertEqual(details["instructions"], "Read only")
        result = approval_request(self.config.state_dir, {
            "action": "resolve", "task_id": submitted["task_id"],
            "decision": "reject", "reason": "Test rejection",
            "approver_identity": "local:test-operator",
        })
        self.assertEqual(result["status"], "rejected")
        self.popen.assert_not_called()

    def test_details_and_history_use_active_coordinator(self):
        submitted = self.controller.submit_task(
            "request-2", "Review details", "Complete instructions", "ina", True,
        )
        details = approval_request(self.config.state_dir, {
            "action": "details", "task_id": submitted["task_id"],
        })
        self.assertEqual(details["instructions"], "Complete instructions")
        history = approval_request(self.config.state_dir, {
            "action": "history", "limit": 10,
        })
        self.assertEqual(history["approvals"][0]["task_id"], submitted["task_id"])

    def test_channel_files_are_owner_only_and_token_is_required(self):
        self.assertEqual(self.channel.socket_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.channel.token_path.stat().st_mode & 0o777, 0o600)
        original = self.channel.token_path.read_text()
        self.channel.token_path.write_text("wrong\n")
        os.chmod(self.channel.token_path, 0o600)
        with self.assertRaisesRegex(RuntimeError, "invalid local approval capability"):
            approval_request(self.config.state_dir, {
                "action": "review", "task_id": "11111111-1111-1111-1111-111111111111",
            })
        self.channel.token_path.write_text(original)
        os.chmod(self.channel.token_path, 0o600)


class ServerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        ids = iter([
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ])
        config = Config(
            "owner/repo", self.root, Path(self.temp.name) / "state", "origin/main",
            {"ina": Worker("ina", ("codex",))},
            Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
        )
        self.controller = TaskController(
            config, popen=Mock(return_value=FakeProcess()),
            workspace_preparer=lambda *_: self.root, id_factory=lambda: next(ids),
        )
        self.server = MCPServer(self.controller)
        self.call_id = 0

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def call(self, name, arguments=None):
        self.call_id += 1
        response = self.server.handle({
            "jsonrpc": "2.0", "id": self.call_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        self.assertFalse(response["result"]["isError"], response)
        return response["result"]["structuredContent"]

    def test_submit_task_published_schema_requires_repository_profile(self):
        submit_task = next(tool for tool in TOOLS if tool["name"] == "submit_task")
        schema = submit_task["inputSchema"]
        self.assertIn("repository_profile", schema["required"])
        self.assertEqual(schema["properties"]["repository_profile"]["type"], "string")
        self.assertEqual(schema["properties"]["consequence_class"]["default"], "unclassified")
        self.assertEqual(set(schema["properties"]["consequence_class"]["enum"]), {
            "routine_development", "production_deployment", "destructive_operation",
            "security_change", "unclassified",
        })

    def test_multi_repository_health_and_submission_scope_through_mcp(self):
        archive = Path(self.temp.name) / "archive"
        archive.mkdir()
        self.controller.close()
        popen = Mock(return_value=FakeProcess())
        self.controller = TaskController(
            Config("owner/repo", self.root, Path(self.temp.name) / "multi-state", "origin/main",
                   {"ina": Worker("ina", ("codex",))},
                   Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                   repository_profiles={
                       "orchestra": RepositoryProfile("orchestra", "owner/repo", self.root, "origin/main"),
                       "archive": RepositoryProfile("archive", "owner/archive", archive, "origin/main"),
                   },
                   default_profile="orchestra"),
            popen=popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )
        self.server = MCPServer(self.controller)

        status = self.call("bridge_status")
        self.assertEqual(status["default_profile"], "orchestra")
        self.assertEqual(status["repository_profiles"], ["archive", "orchestra"])

        missing_profile = self.server.handle({
            "jsonrpc": "2.0", "id": 100, "method": "tools/call",
            "params": {"name": "submit_task", "arguments": {
                "request_id": "missing-profile", "title": "Controlled task",
                "instructions": "Perform bounded work", "worker": "ina",
                "requires_approval": True,
            }},
        })
        self.assertTrue(missing_profile["result"]["isError"])
        self.assertIn("repository_profile is required", missing_profile["result"]["content"][0]["text"])
        self.assertEqual(self.controller.state["tasks"], {})

        submitted = self.call("submit_task", {
            "request_id": "explicit-profile", "title": "Controlled task",
            "instructions": "Perform bounded work", "worker": "ina",
            "repository_profile": "archive", "requires_approval": True,
        })
        self.assertEqual(submitted["repository_profile"], "archive")
        self.assertEqual(submitted["repository"], "owner/archive")
        self.assertEqual(submitted["status"], "approval_required")
        popen.assert_not_called()

    def test_every_task_control_tool_through_mcp_boundary(self):
        self.assertEqual(self.call("bridge_status")["status"], "ok")
        submitted = self.call("submit_task", {
            "request_id": "mcp-request-1", "title": "Controlled task",
            "instructions": "Perform bounded work", "worker": "ina",
            "repository_profile": "default", "requires_approval": True,
        })
        task_id = submitted["task_id"]
        self.assertEqual(self.call("task_status", {"task_id": task_id})["status"], "approval_required")
        self.assertEqual(len(self.call("list_tasks")["tasks"]), 1)
        self.assertEqual(len(self.call("list_approvals")["approvals"]), 1)
        self.assertTrue(self.call("worker_availability")["workers"][0]["available"])
        self.assertEqual(self.call("cancel_task", {
            "task_id": task_id, "reason": "Requester withdrew it",
        })["status"], "cancelled")
        self.assertEqual(self.call("task_result", {"task_id": task_id})["result"], "")

        self.assertNotIn("resolve_approval", {tool.get("name") for tool in TOOLS})
        gated = self.call("submit_task", {
            "request_id": "mcp-request-2", "title": "Approval decision",
            "instructions": "Wait for a decision", "worker": "ina",
            "repository_profile": "default", "requires_approval": True,
        })
        self.assertEqual(gated["status"], "approval_required")


class StdioIntegrationTests(unittest.TestCase):
    def test_server_negotiates_and_exposes_tools_over_stdio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            config_path = Path(directory) / "omcp.json"
            config_path.write_text(json.dumps({
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(Path(directory) / "state"),
                "workers": {"ina": {"command": ["codex"]}},
                "principal": {
                    "identity": "chatgpt:test", "authentication": "test-stdio",
                    "permissions": sorted(ALL_PERMISSIONS),
                },
            }))
            os.chmod(config_path, 0o600)
            requests = "\n".join(json.dumps(item) for item in [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "bridge_status", "arguments": {}}},
            ]) + "\n"
            environment = {
                **os.environ,
                "OMCP_TUNNEL_AUTHENTICATED": "1",
                "OMCP_AUTHENTICATED_CALLER": "chatgpt:test",
            }
            completed = subprocess.run(
                [sys.executable, "-m", "omcp.server", "--config", str(config_path)],
                input=requests, text=True, capture_output=True, check=False,
                cwd=Path(__file__).resolve().parents[1], timeout=10, env=environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual([response["id"] for response in responses], [1, 2, 3])
            self.assertEqual(len(responses[1]["result"]["tools"]), 8)
            self.assertEqual(responses[2]["result"]["structuredContent"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
