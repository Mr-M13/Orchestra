import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from omcp.controller import Config, Principal, TaskController, Worker
from omcp.github_actions import (
    DispatchIdentity, GitHubActionsClient, GitHubActionsError, decode_instructions,
    encode_instructions,
)
from omcp.github_runner import _apply_patch, run as run_github_worker
from omcp.controller import deepseek_runner_command
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
WORKFLOW_SHA = "a" * 40


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

    def test_seatbelt_sandbox_emits_only_exact_loopback_ports(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("omcp.platform.host_platform", return_value="darwin"), \
                patch("omcp.platform.sandbox_executable",
                      return_value="/usr/bin/sandbox-exec"):
            workspace = Path(directory)
            command = sandbox_command(SandboxPolicy(
                read_paths=(workspace,), write_paths=(workspace,),
                loopback_bind_ports=(49153,), loopback_connect_ports=(49152,),
                backend="seatbelt", workdir=workspace,
            ), ["/usr/bin/true"])
        profile = command[2]
        self.assertIn('(allow process-exec (literal "/usr/bin/true"))', profile)
        self.assertIn('(local tcp "localhost:49153")', profile)
        self.assertIn('(remote tcp "localhost:49152")', profile)
        self.assertNotIn("localhost:*", profile)
        self.assertNotIn("*:443", profile)
        self.assertNotIn("api.deepseek.com", profile)

    def test_linux_sandbox_rejects_unfiltered_loopback_policy(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("omcp.platform.host_platform", return_value="linux"), \
                patch("omcp.platform.sandbox_executable", return_value="/usr/bin/bwrap"):
            workspace = Path(directory)
            with self.assertRaisesRegex(SandboxUnavailable, "exact loopback-port"):
                sandbox_command(SandboxPolicy(
                    write_paths=(workspace,), loopback_connect_ports=(49152,),
                    backend="bubblewrap", workdir=workspace,
                ), ["python3", "-V"])

    def test_sandbox_selection_fails_closed_on_an_unsupported_backend(self):
        with patch("omcp.platform.host_platform", return_value="linux"):
            self.assertEqual(sandbox_backend("bubblewrap"), "bubblewrap")
            with self.assertRaises(SandboxUnavailable):
                sandbox_backend("seatbelt")
            report = describe_sandbox("seatbelt")
        self.assertFalse(report["available"])

    def test_systemd_sandbox_is_a_linux_fallback(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("omcp.platform.host_platform", return_value="linux"), \
                patch("omcp.platform.sandbox_executable", return_value="/usr/bin/systemd-run"):
            workspace = Path(directory).resolve()
            command = sandbox_command(SandboxPolicy(
                read_paths=(workspace,), write_paths=(workspace,),
                backend="systemd", workdir=workspace,
            ), ["/usr/bin/true"])
        self.assertEqual(command[0], "/usr/bin/systemd-run")
        self.assertIn("--property=ProtectSystem=strict", command)
        self.assertIn("--property=RestrictAddressFamilies=AF_UNIX", command)
        self.assertIn("--property=BindReadOnlyPaths=" + str(workspace) + ":" + str(workspace), command)
        self.assertIn("--property=BindPaths=" + str(workspace) + ":" + str(workspace), command)
        self.assertEqual(command[-2:], ["--", "/usr/bin/true"])

    def test_direct_deepseek_uses_the_configured_sandbox_backend(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("omcp.platform.host_platform", return_value="linux"), \
                patch("omcp.platform.sandbox_executable", return_value="/usr/bin/systemd-run"):
            root = Path(directory)
            spec = root / "runner.json"; spec.write_text("{}")
            credential = root / "credential"; credential.write_text("test")
            with self.assertRaisesRegex(SandboxUnavailable, "IP allowlist"):
                deepseek_runner_command(spec, root, root, credential, "systemd")


class GitHubConfigTests(unittest.TestCase):
    def config(self, directory: Path, worker: dict, **extra):
        root = directory / "project"
        root.mkdir(exist_ok=True)
        worker.setdefault("github_dispatch_socket", str(directory / "dispatch.sock"))
        worker.setdefault("github_service_uid", os.geteuid() + 1)
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
            })
            with patch("omcp.github_dispatch_service.ensure_dispatch_socket"):
                config = Config.load(path)
        worker = config.workers["actions"]
        self.assertEqual(worker.adapter, "github_actions")
        self.assertEqual(worker.agent, "deepseek")
        self.assertEqual(worker.model, "deepseek-chat")
        self.assertEqual(worker.command, ())
        self.assertIsNone(worker.github_repository)
        self.assertIsNone(worker.workflow_ref)
        self.assertIsNone(worker.workflow_sha)
        self.assertEqual(worker.github_service_uid, os.geteuid() + 1)
        self.assertTrue(worker.github_dispatch_socket.endswith("dispatch.sock"))

    def test_github_actions_worker_rejects_local_commands_and_unsafe_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.config(Path(directory), {
                "agent": "codex", "command": ["/bin/sh"],
            })
            with self.assertRaisesRegex(ValueError, "may not configure a local command"):
                Config.load(path)
            path = self.config(Path(directory), {
                "agent": "codex", "workflow_ref": "main",
            })
            with self.assertRaisesRegex(ValueError, "belong only in the dispatch broker"):
                Config.load(path)
            path = self.config(Path(directory), {
                "agent": "codex", "workflow_sha": "main",
            })
            with self.assertRaisesRegex(ValueError, "belong only in the dispatch broker"):
                Config.load(path)
            path = self.config(Path(directory), {
                "agent": "codex", "github_service_uid": os.geteuid(),
            })
            with self.assertRaisesRegex(ValueError, "dedicated identity"):
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
            with patch("omcp.github_dispatch_service.ensure_dispatch_socket"):
                with self.assertRaisesRegex(ValueError, "only name Codex workers"):
                    Config.load(path)


class GitHubActionsClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.token = Path(self.temp.name) / "token"
        self.token.write_text("secret-token-value\n")
        self.token.chmod(0o600)
        self.requests = []
        self.identity = DispatchIdentity(
            "11111111-1111-1111-1111-111111111111",
            "request-1",
            "22222222-2222-4222-8222-222222222222",
            "2026-10-20T00:00:00+00:00",
            WORKFLOW_SHA,
            "deepseek", "deepseek-chat", hashlib.sha256(
                encode_instructions("Implement the task").encode("ascii")
            ).hexdigest(),
        )

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
            "owner/repo", "orchestra-worker.yml", "main", WORKFLOW_SHA, self.token,
            opener=self.opener([
                json.dumps({
                    "name": "main", "protected": True,
                    "commit": {"sha": WORKFLOW_SHA},
                }).encode(),
                json.dumps({
                    "name": "orchestra-workers",
                    "protection_rules": [],
                    "deployment_branch_policy": {"protected_branches": True},
                }).encode(),
                json.dumps({
                    "workflow_run_id": 7,
                    "run_url": "https://api.github.com/repos/owner/repo/actions/runs/7",
                    "html_url": "https://github.com/owner/repo/actions/runs/7",
                }).encode(),
                json.dumps({"workflow_runs": [{
                    "id": 7, "display_title": self.identity.run_name,
                    "event": "workflow_dispatch", "head_sha": WORKFLOW_SHA,
                    "head_branch": "main", "created_at": "2026-10-20T00:00:01Z",
                    "path": ".github/workflows/orchestra-worker.yml@main",
                    "run_attempt": 1, "actor": {"login": "orchestra-bot"},
                    "triggering_actor": {"login": "orchestra-bot"},
                    "status": "queued", "html_url": "https://example/run",
                }]}).encode(),
            ]),
            dispatch_actor="orchestra-bot",
        )
        client.dispatch(
            self.identity, "deepseek", "deepseek-chat", "Implement the task",
        )
        verification = self.requests[0]
        self.assertEqual(verification.method, "GET")
        self.assertIn("/branches/main", verification.full_url)
        environment = self.requests[1]
        self.assertEqual(environment.method, "GET")
        self.assertIn("/environments/orchestra-workers", environment.full_url)
        request = self.requests[2]
        self.assertEqual(request.method, "POST")
        self.assertIn("/actions/workflows/orchestra-worker.yml/dispatches", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token-value")
        body = json.loads(request.data)
        self.assertEqual(body["ref"], "main")
        self.assertEqual(body["inputs"]["workflow_sha"], WORKFLOW_SHA)
        self.assertEqual(body["inputs"]["source_sha"], WORKFLOW_SHA)
        self.assertEqual(body["inputs"]["dispatch_binding"], self.identity.binding_sha256)
        self.assertTrue(body["return_run_details"])
        self.assertEqual(body["inputs"]["payload_sha256"], self.identity.payload_sha256)
        self.assertEqual(decode_instructions(body["inputs"]["payload"]), "Implement the task")
        self.assertNotIn(b"secret-token-value", request.data)

    def test_oversized_payload_fails_before_dispatch(self):
        client = GitHubActionsClient(
            "owner/repo", "orchestra-worker.yml", "main", WORKFLOW_SHA, self.token,
            opener=self.opener([]),
        )
        with self.assertRaisesRegex(ValueError, "too large"):
            client.dispatch(
                self.identity, "deepseek", "deepseek-chat",
                base64.b64encode(os.urandom(60_000)).decode("ascii"),
            )
        self.assertEqual(self.requests, [])

    def test_cancellation_uses_only_the_persisted_run_identity(self):
        from omcp.github_actions import cancel_task_run

        task = Path(self.temp.name) / "task"
        task.mkdir()
        (task / "github-state.json").write_text(json.dumps({
            "schema_version": 4,
            "phase": "adopted",
            "run_id": 41,
            "task_id": self.identity.task_id,
            "request_id": self.identity.request_id,
            "dispatch_id": self.identity.dispatch_id,
            "dispatched_at": self.identity.dispatched_at,
            "source_sha": self.identity.source_sha,
            "agent": self.identity.agent,
            "model": self.identity.model,
            "payload_sha256": self.identity.payload_sha256,
        }))
        (task / "github-actions.json").write_text(json.dumps({
            "github_service_uid": os.geteuid() + 1,
            "github_dispatch_socket": str(Path(self.temp.name) / "dispatch.sock"),
        }))
        client = Mock()
        with patch("omcp.github_dispatch_service.DispatchClient", return_value=client):
            result = cancel_task_run(task, reason="operator withdrew it")
        self.assertEqual(result["run_id"], 41)
        client.cancel.assert_called_once_with(41, self.identity)

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
            "owner/repo", "orchestra-worker.yml", "main", WORKFLOW_SHA, self.token,
            opener=self.opener([artifact, buffer.getvalue()]),
        )
        with self.assertRaisesRegex(GitHubActionsError, "unsafe path"):
            client.download_artifact(
                1, "omcp-11111111-1111-1111-1111-111111111111",
            )


class GitHubControllerTests(unittest.TestCase):
    def test_restart_blocks_rollout_when_legacy_run_cannot_be_drained(self):
        with tempfile.TemporaryDirectory() as directory:
            task_id = "11111111-1111-1111-1111-111111111111"
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            state_dir = base / "state"
            task_dir = state_dir / "tasks" / task_id
            task_dir.mkdir(parents=True)
            task = {
                "task_id": task_id, "request_id": "legacy-request",
                "title": "Legacy remote task", "instructions": "old",
                "worker": "actions", "status": "cancelled",
                "created_at": "2026-10-20T00:00:00+00:00",
                "repository": "owner/repo", "repository_profile": "default",
                "resource_requirements": {
                    "worker": "actions", "repository": "owner/repo",
                    "access": "write",
                },
            }
            (state_dir / "state.json").write_text(json.dumps({
                "schema_version": 1, "tasks": {task_id: task},
                "requests": {"legacy-request": task_id},
            }))
            (task_dir / "github-state.json").write_text(json.dumps({
                "schema_version": 2, "phase": "adopted", "run_id": 41,
                "task_id": task_id, "request_id": "legacy-request",
                "dispatch_id": "22222222-2222-4222-8222-222222222222",
                "dispatched_at": "2026-10-20T00:00:00+00:00",
            }))
            (task_dir / "github-actions.json").write_text("{}")
            config = Config(
                "owner/repo", root, state_dir, "origin/main",
                {"actions": Worker(
                    "actions", (), adapter="github_actions", agent="codex",
                    github_service_uid=os.geteuid() + 1,
                    github_dispatch_socket=str(base / "dispatch.sock"),
                )},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
            )
            with patch(
                "omcp.github_actions.drain_legacy_task_run",
                side_effect=GitHubActionsError("broker unavailable"),
            ), self.assertRaisesRegex(RuntimeError, "must be drained"):
                TaskController(config)
            audit = (state_dir / "audit.jsonl").read_text()
            self.assertIn('"outcome":"blocked"', audit)

    def test_controller_dispatches_actions_worker_without_exposing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "tracked.txt").write_text("base\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            source_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            ).strip()
            config = Config(
                "owner/repo", root, base / "state", "origin/main",
                {"actions": Worker(
                    "actions", (), adapter="github_actions",
                    agent="codex", model="gpt-5-codex",
                    github_service_uid=os.geteuid() + 1,
                    github_dispatch_socket="/run/orchestra-github-dispatch/dispatch.sock",
                )},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
            )
            popen = Mock(return_value=FakeProcess())
            workspace_preparer = Mock(return_value=root)
            controller = TaskController(
                config, popen=popen, workspace_preparer=workspace_preparer,
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
                self.assertEqual(spec["github_service_uid"], os.geteuid() + 1)
                self.assertEqual(spec["github_dispatch_socket"],
                                 "/run/orchestra-github-dispatch/dispatch.sock")
                self.assertEqual(spec["source_sha"], source_sha)
                self.assertNotIn("credential", json.dumps(spec))
                self.assertNotIn("github_repository", spec)
                self.assertNotIn("workflow", spec)
                self.assertEqual(approved["remote_artifact"], "omcp-" + submitted["task_id"])
                workspace_preparer.assert_called_once_with(
                    submitted["task_id"], "omcp/task-11111111",
                    config.profile("default"), False,
                )
            finally:
                controller.close()


class GitHubRunnerTests(unittest.TestCase):
    def test_patch_application_rejects_a_drifted_local_source(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "workspace"
            task = base / "task"
            workspace.mkdir()
            task.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
            (workspace / "file.txt").write_text("before\n")
            subprocess.run(["git", "add", "file.txt"], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
            patch_text = (
                "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
                "@@ -1 +1 @@\n-before\n+after\n"
            ).encode()
            with self.assertRaisesRegex(RuntimeError, "source SHA"):
                _apply_patch(workspace, task, patch_text, 1024, "b" * 40)
            self.assertEqual((workspace / "file.txt").read_text(), "before\n")

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
            source_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=workspace, text=True,
            ).strip()
            task = base / "task"
            task.mkdir()
            prompt = task / "prompt.txt"
            prompt.write_text("task")
            spec = task / "github-actions.json"
            spec.write_text(json.dumps({
                "agent": "codex", "model": "gpt-5-codex",
                "github_service_uid": os.geteuid() + 1,
                "github_dispatch_socket": str(base / "dispatch.sock"),
                "task_id": "11111111-1111-1111-1111-111111111111",
                "request_id": "request-1", "prompt_path": str(prompt),
                "log_path": str(task / "worker.log"), "completion_path": str(task / "completion.json"),
                "result_path": str(task / "result.txt"), "workspace": str(workspace),
                "remote_state_path": str(task / "github-state.json"),
                "artifact_name": "omcp-11111111-1111-1111-1111-111111111111",
                "max_worker_seconds": 60, "max_response_bytes": 1024,
                "poll_seconds": 2, "lease_id": "22222222-2222-2222-2222-222222222222",
                "lease_path": str(task / "lease.json"),
                "source_sha": source_sha,
            }))
            patch_text = (
                "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
                "@@ -1 +1 @@\n-before\n+after\n"
            ).encode()
            fake = Mock()
            fake.dispatch.return_value = Mock(
                run_id=7, status="queued", conclusion=None, url="u",
            )
            fake.status.side_effect = [Mock(run_id=7, status="completed", conclusion="success", url="u")]
            fake.download_artifact.return_value = {
                "result.txt": b"remote result\n", "patch.diff": patch_text,
            }
            with patch("omcp.github_runner.DispatchClient", return_value=fake):
                self.assertEqual(run_github_worker(spec), 0)
            self.assertEqual((workspace / "file.txt").read_text(), "after\n")
            self.assertEqual((task / "result.txt").read_text(), "remote result\n")
            completion = json.loads((task / "completion.json").read_text())
            self.assertEqual(completion["exit_code"], 0)


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
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
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
        self.assertEqual(set(example["workers"]), {
            "ina-codex", "hamed-codex", "deepseek-primary", "deepseek-secondary",
        })
        self.assertEqual(example["task_approval_mode"], "disabled")
        self.assertEqual(example["local_worker_launcher"], "systemd-user")
        self.assertNotEqual(
            Path(example["state_dir"]), Path(example["project_root"]),
        )
        unit = (root / "deploy/pixelbox/systemd/orchestra-omcp.service").read_text()
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("ReadWritePaths=/home/hamed/orchestra-migration/state", unit)
        self.assertEqual(unit.lower().count("execstart="), 1)
        self.assertIn("execstart=/usr/bin/python3 -m omcp.server", unit.lower())
        self.assertIn("KillMode=process", unit)
        self.assertNotIn("KillMode=control-group", unit)
        self.assertNotIn("docker", unit.lower())
        self.assertNotIn("systemctl", unit.lower())
        self.assertTrue(all(
            worker.get("adapter") != "github_actions"
            for worker in example["workers"].values()
        ))
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
            now = datetime.now(timezone.utc).isoformat()
            (state / "state.json").write_text(json.dumps({
                "schema_version": 1, "requests": {},
                "scheduler": {"last_repository": None},
                "tasks": {
                    "11111111-1111-1111-1111-111111111111": {
                        "task_id": "11111111-1111-1111-1111-111111111111",
                        "status": "succeeded", "worker": "ina",
                        "created_at": now, "finished_at": now,
                    },
                },
            }))
            (state / "state.json").chmod(0o600)
            (state / "audit.jsonl").write_text("")
            (state / "audit.jsonl").chmod(0o600)
            (state / "heartbeat.json").write_text(json.dumps({
                "pid": os.getpid(),
                "last_tick": now,
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
        self.assertEqual(report["checks"]["dashboard"]["status"], "unchecked")
        self.assertEqual(report["checks"]["mcp_gateway"]["status"], "unchecked")
        self.assertNotIn("instructions", json.dumps(report))

    def test_health_surfaces_stale_and_startup_failures_without_task_content(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            codex = base / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            now = datetime.now(timezone.utc).isoformat()
            (state / "state.json").write_text(json.dumps({
                "schema_version": 1,
                "requests": {},
                "scheduler": {"last_repository": None},
                "tasks": {
                    "11111111-1111-1111-1111-111111111111": {
                        "task_id": "11111111-1111-1111-1111-111111111111",
                        "status": "accepted", "created_at": old, "worker": "ina",
                        "instructions": "private task content",
                    },
                    "22222222-2222-2222-2222-222222222222": {
                        "task_id": "22222222-2222-2222-2222-222222222222",
                        "status": "failed", "created_at": now, "finished_at": now,
                        "worker": "ina", "failure": {"type": "runner_startup_failed"},
                        "instructions": "private task content",
                    },
                    "33333333-3333-3333-3333-333333333333": {
                        "task_id": "33333333-3333-3333-3333-333333333333",
                        "status": "failed", "created_at": now, "finished_at": now,
                        "worker": "ina",
                        "failure": {
                            "type": "CreditExhausted", "category": "credit_exhausted",
                            "stage": "provider",
                        },
                        "instructions": "private task content",
                    },
                },
            }))
            (state / "state.json").chmod(0o600)
            (state / "audit.jsonl").write_text("")
            (state / "audit.jsonl").chmod(0o600)
            (state / "heartbeat.json").write_text(json.dumps({
                "pid": os.getpid(),
                "last_tick": now,
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
        self.assertEqual(report["status"], "degraded")
        self.assertIn("stale_accepted_tasks", report["warnings"])
        self.assertIn("readiness_unavailable", report["warnings"])
        for historical in (
            "runner_startup_failures", "failed_tasks_present",
            "credit_exhausted_tasks", "failed_actions_dispatch",
        ):
            self.assertNotIn(historical, report["warnings"])
        self.assertEqual(report["checks"]["tasks"]["recent_credit_exhausted"], 1)
        self.assertEqual(
            report["checks"]["tasks"]["failure_categories"]["credit_exhausted"], 1,
        )
        self.assertEqual(report["checks"]["readiness"]["status"], "unavailable")
        self.assertNotIn("private task content", json.dumps(report))

    def test_health_uses_verified_backup_status_and_warns_on_failure(self):
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
            with patch("omcp.health.backup_health", return_value={
                    "checked": True, "status": "failed", "reason": "backup_overdue",
                    "operator_action": "Run the backup."}) as backup_check, \
                    patch("omcp.controller.shutil.which", return_value=str(codex)), \
                    patch("omcp.platform.sandbox_executable", return_value="/usr/bin/bwrap"), \
                    patch("omcp.platform.host_platform", return_value="linux"):
                report = collect_health(config_path, backup_config="/temporary/backup.json")
            backup_check.assert_called_once_with(Path("/temporary/backup.json"))
        self.assertEqual(report["checks"]["backup"]["status"], "failed")
        self.assertIn("backup_failed", report["warnings"])

    def test_health_backup_check_does_not_treat_unrelated_archives_as_healthy(self):
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
            backup = base / "backup"
            backup.mkdir()
            (backup / "unrelated-source-snapshot.tar.gz").write_text("not a state backup")
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
                report = collect_health(config_path, backup_dir=str(backup))
        self.assertEqual(report["checks"]["backup"]["status"], "unverified")
        self.assertFalse(report["checks"]["backup"]["checked"])
        self.assertNotIn("backup_archive_missing", report["warnings"])


if __name__ == "__main__":
    unittest.main()
