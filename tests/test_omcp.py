import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from omcp.approval import format_review
from omcp.controller import AuthorizationError, Config, Principal, TaskController, Worker
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
            workspace_preparer=lambda _task_id, _branch: self.root,
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

    def test_every_task_control_tool_through_mcp_boundary(self):
        self.assertEqual(self.call("bridge_status")["status"], "ok")
        submitted = self.call("submit_task", {
            "request_id": "mcp-request-1", "title": "Controlled task",
            "instructions": "Perform bounded work", "worker": "ina",
            "requires_approval": True,
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
            "requires_approval": True,
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
