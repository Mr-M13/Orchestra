import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from omcp.controller import AuthorizationError, Config, Principal, TaskController, Worker
from omcp.runner import run as run_worker
from omcp.server import MCPServer, TOOLS


ALL_PERMISSIONS = frozenset({
    "status:read", "tasks:submit", "tasks:read", "results:read",
    "workers:read", "tasks:cancel", "approvals:read", "approvals:decide",
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

    def submit(self, request_id="request-1", approval=False):
        return self.controller.submit_task(
            request_id, "Implement feature", "Make the requested change", "ina", approval,
        )

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
        submitted = self.submit(approval=True)
        self.assertEqual(submitted["status"], "approval_required")
        self.assertEqual(len(self.controller.list_approvals()["approvals"]), 1)
        result = self.controller.resolve_approval(submitted["task_id"], "reject", "Not authorised")
        self.assertEqual(result["status"], "rejected")
        self.popen.assert_not_called()

    def test_approval_gate_launches_only_after_approval(self):
        submitted = self.submit(approval=True)
        result = self.controller.resolve_approval(submitted["task_id"], "approve", "Scope reviewed")
        self.assertEqual(result["status"], "running")
        self.popen.assert_called_once()

    def test_pending_task_can_be_cancelled_with_reason(self):
        submitted = self.submit(approval=True)
        result = self.controller.cancel_task(submitted["task_id"], "No longer needed")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["cancel_reason"], "No longer needed")

    def test_list_tasks_filters_and_bounds_results(self):
        submitted = self.submit(approval=True)
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
    def test_config_requires_owner_only_file_and_rejects_unsafe_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            config_path = Path(directory) / "omcp.json"
            raw = {
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(Path(directory) / "state"),
                "workers": {"ina": {"command": ["codex", "--approve-for-me"]}},
                "principal": {"identity": "chatgpt", "permissions": ["status:read"]},
            }
            config_path.write_text(json.dumps(raw))
            os.chmod(config_path, 0o600)
            with self.assertRaisesRegex(ValueError, "bypasses"):
                Config.load(config_path)
            os.chmod(config_path, 0o644)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                Config.load(config_path)


class RunnerTests(unittest.TestCase):
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
            self.assertIn("--sandbox", execute.call_args.args[0])
            self.assertIn("workspace-write", execute.call_args.args[0])


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

        gated = self.call("submit_task", {
            "request_id": "mcp-request-2", "title": "Approval decision",
            "instructions": "Wait for a decision", "worker": "ina",
            "requires_approval": True,
        })
        rejected = self.call("resolve_approval", {
            "task_id": gated["task_id"], "decision": "reject", "reason": "Scope changed",
        })
        self.assertEqual(rejected["status"], "rejected")


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
            completed = subprocess.run(
                [sys.executable, "-m", "omcp.server", "--config", str(config_path)],
                input=requests, text=True, capture_output=True, check=False,
                cwd=Path(__file__).resolve().parents[1], timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual([response["id"] for response in responses], [1, 2, 3])
            self.assertEqual(len(responses[1]["result"]["tools"]), 9)
            self.assertEqual(responses[2]["result"]["structuredContent"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
