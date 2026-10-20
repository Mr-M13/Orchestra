import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
import zlib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from omcp.controller import Config, Principal, TaskController, Worker
from omcp.github_actions import (
    GitHubActionsClient, GitHubActionsError, decode_instructions,
    encode_instructions,
)
from omcp.github_runner import run as run_github_worker
from omcp.health import collect_health
from omcp.platform import (
    SandboxPolicy, SandboxUnavailable, sandbox_backend, sandbox_command,
    describe_sandbox,
)


ALL_PERMISSIONS = frozenset({
    "status:read", "tasks:submit", "tasks:read", "results:read",
    "workers:read", "tasks:cancel", "approvals:read",
    "host_operations:submit",
})


class FakeProcess:
    pid = os.getpid()


class Response:
    def __init__(self, payload=b"{}", status=200):
        self.payload = payload
        self.status = status

    def read(self, _limit=-1):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class PlatformTests(unittest.TestCase):
    def test_linux_sandbox_uses_bubblewrap_and_denies_network_by_default(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("omcp.platform.host_platform", return_value="linux"), \
                patch("omcp.platform.sandbox_executable", return_value="/usr/bin/bwrap"):
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            policy = SandboxPolicy(
                read_paths=(workspace,), write_paths=(workspace,),
                workdir=workspace,
            )
            command = sandbox_command(policy, ["python3", "-V"])
        self.assertEqual(command[0], "/usr/bin/bwrap")
        self.assertIn("--unshare-net", command)
        self.assertIn("--die-with-parent", command)
        self.assertEqual(command[-3:], ["--", "python3", "-V"])

    def test_linux_sandbox_shares_network_only_for_an_explicit_provider_policy(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("omcp.platform.host_platform", return_value="linux"), \
                patch("omcp.platform.sandbox_executable", return_value="/usr/bin/bwrap"):
            workspace = Path(directory)
            policy = SandboxPolicy(
                read_paths=(workspace,), write_paths=(workspace,),
                network_hosts=("api.deepseek.com",), workdir=workspace,
            )
            command = sandbox_command(policy, ["python3", "-V"])
        self.assertIn("--share-net", command)
        self.assertNotIn("--unshare-net", command)

    def test_sandbox_selection_fails_closed_on_an_unsupported_backend(self):
        with patch("omcp.platform.host_platform", return_value="linux"):
            self.assertEqual(sandbox_backend("bubblewrap"), "bubblewrap")
            with self.assertRaises(SandboxUnavailable):
                sandbox_backend("seatbelt")
            report = describe_sandbox("seatbelt")
        self.assertFalse(report["available"])


class GitHubConfigTests(unittest.TestCase):
    def config(self, directory: Path, worker: dict, **extra):
        root = directory / "project"
        root.mkdir(exist_ok=True)
        token = directory / "github.token"
        token.write_text("never-print-this\n")
        token.chmod(0o600)
        worker.setdefault("credential_file", str(token))
        raw = {
            "repository": "owner/repo", "project_root": str(root),
            "state_dir": str(directory / "state"),
            "workers": {"actions": {"adapter": "github_actions", **worker}},
            "principal": {"identity": "chatgpt:test", "permissions": ["tasks:submit"]},
        }
        raw.update(extra)
        path = directory / "omcp.json"
        path.write_text(json.dumps(raw))
        path.chmod(0o600)
        return path

    def test_github_actions_worker_is_owner_configured_and_has_no_local_command(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.config(Path(directory), {
                "agent": "deepseek", "model": "deepseek-chat",
                "workflow": "orchestra-worker.yml", "workflow_ref": "main",
            })
            config = Config.load(path)
        worker = config.workers["actions"]
        self.assertEqual(worker.adapter, "github_actions")
        self.assertEqual(worker.agent, "deepseek")
        self.assertEqual(worker.model, "deepseek-chat")
        self.assertEqual(worker.command, ())
        self.assertEqual(worker.github_repository, "owner/repo")

    def test_github_actions_worker_rejects_local_commands_and_unsafe_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.config(Path(directory), {
                "agent": "codex", "command": ["/bin/sh"],
            })
            with self.assertRaisesRegex(ValueError, "may not configure a local command"):
                Config.load(path)
            path = self.config(Path(directory), {
                "agent": "codex", "workflow_ref": "../main",
            })
            with self.assertRaisesRegex(ValueError, "workflow_ref"):
                Config.load(path)
            path = self.config(Path(directory), {"agent": "shell"})
            with self.assertRaisesRegex(ValueError, "agent must be codex or deepseek"):
                Config.load(path)

    def test_github_actions_worker_cannot_receive_standing_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.config(Path(directory), {"agent": "codex"}, standing_authorizations=[{
                "id": "routine", "consequence_class": "routine_development",
                "repository_profiles": ["default"], "workers": ["actions"],
            }])
            with self.assertRaisesRegex(ValueError, "only name Codex workers"):
                Config.load(path)


class GitHubActionsClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.token = Path(self.temp.name) / "token"
        self.token.write_text("secret-token-value\n")
        self.token.chmod(0o600)
        self.requests = []

    def tearDown(self):
        self.temp.cleanup()

    def opener(self, payloads):
        iterator = iter(payloads)

        def open_request(request, timeout=30):
            self.requests.append(request)
            return Response(next(iterator))

        return open_request

    def test_dispatch_uses_header_auth_and_encoded_payload(self):
        client = GitHubActionsClient(
            "owner/repo", "orchestra-worker.yml", "main", self.token,
            opener=self.opener([b"{}"]),
        )
        client.dispatch(
            "11111111-1111-1111-1111-111111111111", "request-1",
            "deepseek", "deepseek-chat", "Implement the task",
        )
        request = self.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertIn("/actions/workflows/orchestra-worker.yml/dispatches", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token-value")
        body = json.loads(request.data)
        self.assertEqual(body["ref"], "main")
        self.assertEqual(decode_instructions(body["inputs"]["payload"]), "Implement the task")
        self.assertNotIn(b"secret-token-value", request.data)

    def test_run_discovery_uses_the_deterministic_run_name(self):
        payload = json.dumps({"workflow_runs": [
            {"id": 1, "display_title": "Other", "status": "completed"},
            {"id": 2, "display_title": "Orchestra 11111111-1111-1111-1111-111111111111",
             "status": "queued", "html_url": "https://example/run"},
        ]}).encode()
        client = GitHubActionsClient(
            "owner/repo", "orchestra-worker.yml", "main", self.token,
            opener=self.opener([payload]),
        )
        found = client.find_run("11111111-1111-1111-1111-111111111111")
        self.assertIsNotNone(found)
        self.assertEqual(found.run_id, 2)
        self.assertEqual(found.status, "queued")

    def test_oversized_payload_fails_before_dispatch(self):
        client = GitHubActionsClient(
            "owner/repo", "orchestra-worker.yml", "main", self.token,
            opener=self.opener([]),
        )
        with self.assertRaisesRegex(ValueError, "too large"):
            client.dispatch(
                "11111111-1111-1111-1111-111111111111", "request-1",
                "codex", "gpt-5-codex",
                base64.b64encode(os.urandom(60_000)).decode("ascii"),
            )
        self.assertEqual(self.requests, [])

    def test_cancellation_uses_only_the_persisted_run_identity(self):
        from omcp.github_actions import cancel_task_run

        task = Path(self.temp.name) / "task"
        task.mkdir()
        (task / "github-state.json").write_text(json.dumps({"run_id": 41}))
        (task / "github-actions.json").write_text(json.dumps({
            "github_repository": "owner/repo", "workflow": "orchestra-worker.yml",
            "workflow_ref": "main", "credential_file": str(self.token),
        }))
        client = Mock()
        with patch("omcp.github_actions.client_from_spec", return_value=client):
            result = cancel_task_run(task, reason="operator withdrew it")
        self.assertEqual(result["run_id"], 41)
        client.cancel.assert_called_once_with(41)

    def test_artifact_rejects_unsafe_paths(self):
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../escape", "bad")
        artifact = json.dumps({"artifacts": [{
            "name": "omcp-11111111-1111-1111-1111-111111111111",
            "archive_download_url": "https://api.github.com/repos/owner/repo/actions/artifacts/1/zip",
        }]}).encode()
        client = GitHubActionsClient(
            "owner/repo", "orchestra-worker.yml", "main", self.token,
            opener=self.opener([artifact, buffer.getvalue()]),
        )
        with self.assertRaisesRegex(GitHubActionsError, "unsafe path"):
            client.download_artifact(
                1, "omcp-11111111-1111-1111-1111-111111111111",
            )


class GitHubControllerTests(unittest.TestCase):
    def test_controller_dispatches_actions_worker_without_exposing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            token = base / "github.token"
            token.write_text("never-log-this-token\n")
            token.chmod(0o600)
            config = Config(
                "owner/repo", root, base / "state", "origin/main",
                {"actions": Worker(
                    "actions", (), adapter="github_actions",
                    credential_file=str(token), github_repository="owner/repo",
                    workflow="orchestra-worker.yml", workflow_ref="main",
                    agent="codex", model="gpt-5-codex",
                )},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
            )
            popen = Mock(return_value=FakeProcess())
            controller = TaskController(
                config, popen=popen, workspace_preparer=lambda *_: root,
                id_factory=lambda: "11111111-1111-1111-1111-111111111111",
            )
            try:
                submitted = controller.submit_task(
                    "actions-1", "Remote task", "Make a bounded change", "actions", True,
                )
                self.assertEqual(submitted["status"], "approval_required")
                approved = controller.resolve_approval(
                    submitted["task_id"], "approve", "Approved for dispatch",
                    "local:test-operator",
                )
                self.assertEqual(approved["status"], "running")
                command = popen.call_args.args[0]
                self.assertIn("omcp.github_runner", command)
                spec = json.loads(
                    (base / "state/tasks" / submitted["task_id"] / "github-actions.json").read_text()
                )
                self.assertEqual(spec["agent"], "codex")
                self.assertEqual(spec["github_repository"], "owner/repo")
                self.assertEqual(spec["workflow"], "orchestra-worker.yml")
                self.assertNotIn("never-log-this-token", json.dumps(spec))
                self.assertNotIn("never-log-this-token", (base / "state/audit.jsonl").read_text())
                self.assertEqual(approved["remote_artifact"], "omcp-" + submitted["task_id"])
            finally:
                controller.close()


class GitHubRunnerTests(unittest.TestCase):
    def test_successful_run_downloads_result_and_applies_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
            (workspace / "file.txt").write_text("before\n")
            subprocess.run(["git", "add", "file.txt"], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
            task = base / "task"
            task.mkdir()
            token = base / "token"
            token.write_text("not-printed\n")
            token.chmod(0o600)
            prompt = task / "prompt.txt"
            prompt.write_text("task")
            spec = task / "github-actions.json"
            spec.write_text(json.dumps({
                "github_repository": "owner/repo", "workflow": "orchestra-worker.yml",
                "workflow_ref": "main", "agent": "codex", "model": "gpt-5-codex",
                "credential_file": str(token), "task_id": "11111111-1111-1111-1111-111111111111",
                "request_id": "request-1", "prompt_path": str(prompt),
                "log_path": str(task / "worker.log"), "completion_path": str(task / "completion.json"),
                "result_path": str(task / "result.txt"), "workspace": str(workspace),
                "remote_state_path": str(task / "github-state.json"),
                "artifact_name": "omcp-11111111-1111-1111-1111-111111111111",
                "max_worker_seconds": 60, "max_response_bytes": 1024,
                "poll_seconds": 2, "lease_id": "22222222-2222-2222-2222-222222222222",
                "lease_path": str(task / "lease.json"),
            }))
            patch_text = (
                "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
                "@@ -1 +1 @@\n-before\n+after\n"
            ).encode()
            fake = Mock()
            fake.find_run.return_value = Mock(run_id=7, status="queued", conclusion=None, url="u")
            fake.status.side_effect = [Mock(run_id=7, status="completed", conclusion="success", url="u")]
            fake.download_artifact.return_value = {
                "result.txt": b"remote result\n", "patch.diff": patch_text,
            }
            with patch("omcp.github_runner.client_from_spec", return_value=fake):
                self.assertEqual(run_github_worker(spec), 0)
            self.assertEqual((workspace / "file.txt").read_text(), "after\n")
            self.assertEqual((task / "result.txt").read_text(), "remote result\n")
            completion = json.loads((task / "completion.json").read_text())
            self.assertEqual(completion["exit_code"], 0)
            self.assertNotIn("not-printed", (task / "completion.json").read_text())


class PersistentSocketTests(unittest.TestCase):
    def test_coordinator_serves_json_rpc_over_owner_only_socket(self):
        import socket
        import threading

        from omcp.server import MCPServer

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            config = Config(
                "owner/repo", root, base / "state", "origin/main",
                {"ina": Worker("ina", ("codex",))},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
            )
            controller = TaskController(config, workspace_preparer=lambda *_: root)
            server = MCPServer(controller)
            socket_path = config.state_dir / "mcp.sock"
            thread = threading.Thread(target=server.run_socket, args=(socket_path,))
            thread.start()
            try:
                deadline = __import__("time").monotonic() + 5
                while not socket_path.exists():
                    if __import__("time").monotonic() > deadline:
                        self.fail("socket was not created")
                    __import__("time").sleep(0.02)
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(5)
                connection.connect(str(socket_path))
                connection.sendall(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
                }).encode() + b"\n")
                response = json.loads(connection.makefile("rb").readline())
                connection.close()
                self.assertEqual(response["id"], 1)
                self.assertEqual(response["result"]["serverInfo"]["name"], "orchestra-omcp")
                self.assertEqual(socket_path.stat().st_mode & 0o777, 0o600)
            finally:
                server.stop_event.set()
                thread.join(timeout=5)
                controller.close()
            self.assertFalse(thread.is_alive())


class DeploymentAssetTests(unittest.TestCase):
    def test_assets_are_present_and_do_not_touch_existing_services(self):
        root = Path(__file__).resolve().parents[1]
        example = json.loads((root / "deploy/pixelbox/omcp.example.json").read_text())
        self.assertEqual(example["sandbox_backend"], "bubblewrap")
        self.assertEqual(example["max_parallel_tasks"], 2)
        self.assertIn("actions-codex", example["workers"])
        self.assertIn("actions-deepseek", example["workers"])
        self.assertNotEqual(
            Path(example["state_dir"]), Path(example["project_root"]),
        )
        unit = (root / "deploy/pixelbox/systemd/orchestra-omcp.service").read_text()
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("ReadWritePaths=/home/hamed/orchestra-migration/state", unit)
        self.assertEqual(unit.lower().count("execstart="), 1)
        self.assertIn("execstart=/usr/bin/python3 -m omcp.server", unit.lower())
        self.assertNotIn("docker", unit.lower())
        self.assertNotIn("systemctl", unit.lower())
        # Workflow implementation is Ina-owned and may be absent from a scoped
        # deployment-review worktree. Validate it when present without making
        # this suite create or alter it.
        workflow_path = root / ".github/workflows/orchestra-worker.yml"
        if workflow_path.exists():
            workflow = workflow_path.read_text()
            self.assertIn("workflow_dispatch:", workflow)
            self.assertIn("contents: read", workflow)
            self.assertIn("persist-credentials: false", workflow)
            self.assertIn("actions/upload-artifact@v4", workflow)
            self.assertNotIn("contents: write", workflow)
        for script in (
            root / "deploy/pixelbox/install.sh",
            root / "deploy/pixelbox/verify.sh",
        ):
            self.assertTrue(os.access(script, os.X_OK), script)
            subprocess.run(["bash", "-n", str(script)], check=True)

    def test_pixelbox_installer_defaults_to_a_dry_run(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            ["bash", str(root / "deploy/pixelbox/install.sh")],
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Dry run complete", completed.stdout)
        self.assertIn("Nextcloud", completed.stdout)


class HealthTests(unittest.TestCase):
    def test_health_report_is_sanitized_and_reports_state_and_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            codex = base / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            (state / "state.json").write_text(json.dumps({
                "schema_version": 1, "tasks": {}, "requests": {},
                "scheduler": {"last_repository": None},
            }))
            (state / "state.json").chmod(0o600)
            (state / "audit.jsonl").write_text("")
            (state / "audit.jsonl").chmod(0o600)
            (state / "heartbeat.json").write_text(json.dumps({
                "pid": os.getpid(),
                "last_tick": datetime.now(timezone.utc).isoformat(),
                "active_tasks": 0, "execution_limit": 2,
            }))
            config_path = base / "omcp.json"
            config_path.write_text(json.dumps({
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(state), "workers": {"ina": {"command": ["codex"]}},
                "principal": {"identity": "chatgpt:test", "permissions": ["status:read"]},
            }))
            config_path.chmod(0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)), \
                    patch("omcp.platform.sandbox_executable", return_value="/usr/bin/bwrap"), \
                    patch("omcp.platform.host_platform", return_value="linux"):
                report = collect_health(config_path)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["checks"]["state"]["ok"])
        self.assertEqual(report["checks"]["sandbox"]["backend"], "bubblewrap")
        self.assertNotIn("instructions", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
