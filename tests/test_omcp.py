import io
from contextlib import contextmanager
from dataclasses import replace
import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import ANY, Mock, patch
from urllib.parse import urlparse

from omcp import worker_contract
from omcp.approval import format_review
from omcp.controller import (
    AuthorizationError, Config, HostOperation, Principal, RepositoryProfile,
    StandingAuthorization, TaskController, UnknownTaskError, Worker, WrongInstanceError,
    systemd_user_worker_command,
)
from omcp.docker_gate import gated_command
from omcp.host_runner import run as run_host_operation
from omcp.local_control import LocalApprovalServer, approval_request
from omcp.runner import (
    OpenCodeDiagnosticCollector, PinnedProviderGateway, WorkerSandboxStartupError,
    opencode_commands, opencode_environment, opencode_policy,
    probe_codex_sandbox, run as run_worker, run_opencode_process,
    worker_log_has_sandbox_startup_failure,
)
from omcp.server import MCPServer, TOOLS, main as server_main


ALL_PERMISSIONS = frozenset({
    "status:read", "tasks:submit", "tasks:read", "results:read",
    "workers:read", "tasks:cancel", "approvals:read",
    "host_operations:submit",
})


class FakeProcess:
    pid = os.getpid()


def codex_fixture(directory: Path) -> dict[str, str]:
    """Return a deterministic PATH containing the executable Config validates."""
    bin_dir = directory / "bin"
    bin_dir.mkdir(exist_ok=True)
    executable = bin_dir / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    return {**os.environ, "PATH": str(bin_dir)}


def codex_success_side_effect(result_path: Path, text: str = "finished"):
    """Simulate Codex writing its mandatory final-message file."""
    def execute(*_args, **_kwargs):
        Path(result_path).write_text(text)
        return Mock(returncode=0)
    return execute


def codex_contract_fixture(
    directory: Path, *, result: str = "worker finished",
    contract: str | None = '{"status":"success"}',
) -> dict[str, str]:
    """Create a fake Codex that writes a result and optional structured files."""
    bin_dir = directory / "bin"
    bin_dir.mkdir(exist_ok=True)
    executable = bin_dir / "codex"
    lines = [
        "#!/bin/sh",
        'while [ "$#" -gt 0 ]; do',
        '  if [ "$1" = "--output-last-message" ]; then shift; result="$1"; fi',
        "  shift",
        "done",
        f"printf '%s\\n' '{result}' > \"$result\"",
    ]
    if contract is not None:
        lines.append(f"printf '%s\\n' '{contract}' > \"$TMPDIR/orchestra-completion.json\"")
    lines.append("exit 0")
    executable.write_text("\n".join(lines) + "\n")
    executable.chmod(0o700)
    return {**os.environ, "PATH": str(bin_dir)}


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
        record = {
            "exit_code": exit_code, "finished_at": "2026-10-11T00:00:00+00:00",
            "runner_error": None,
        }
        if task.get("requires_completion_contract"):
            record["completion_contract"] = "success" if exit_code == 0 else "failure"
        Path(task["completion_path"]).write_text(json.dumps(record))

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

    def test_submission_ack_and_restart_keep_one_instance_identity(self):
        submitted = self.submit()
        identity = (
            submitted["instance_id"], submitted["state_store_id"],
            submitted["instance_role"],
        )
        self.assertEqual(identity[2], "authoritative")
        self.assertRegex(identity[0], r"^[0-9a-f-]{36}$")
        self.assertRegex(identity[1], r"^[0-9a-f]{24}$")
        self.controller.close()
        self.controller = TaskController(
            self.config, popen=self.popen, workspace_preparer=lambda *_: self.root,
        )
        recovered = self.controller.task_status(submitted["task_id"])
        self.assertEqual(
            (recovered["instance_id"], recovered["state_store_id"],
             recovered["instance_role"]),
            identity,
        )
        self.assertEqual(
            self.controller.bridge_status()["instance_id"], identity[0],
        )

    def test_retired_instance_rejects_task_methods_with_explicit_identity(self):
        retired_state = Path(self.temp.name) / "retired-state"
        config = replace(
            self.config, state_dir=retired_state, instance_role="retired",
            authoritative_instance_id="11111111-1111-1111-1111-111111111111",
        )
        self.controller.close()
        self.controller = TaskController(
            config, popen=self.popen, workspace_preparer=lambda *_: self.root,
        )
        with self.assertRaises(WrongInstanceError) as caught:
            self.controller.list_tasks()
        self.assertEqual(
            caught.exception.identity["instance_role"], "retired",
        )
        self.assertEqual(
            caught.exception.identity["authoritative_instance_id"],
            "11111111-1111-1111-1111-111111111111",
        )
        status = self.controller.bridge_status()
        self.assertEqual(status["instance_role"], "retired")
        self.assertNotIn("state_dir", status)

    def test_unknown_task_is_an_explicit_instance_scoped_error(self):
        with self.assertRaises(UnknownTaskError) as caught:
            self.controller.task_status("99999999-9999-4999-8999-999999999999")
        self.assertEqual(
            caught.exception.identity["instance_id"],
            self.controller.bridge_status()["instance_id"],
        )

    def test_local_worker_refreshes_base_ref_before_workspace_creation(self):
        workspace_preparer = Mock(return_value=self.root)
        self.controller.workspace_preparer = workspace_preparer

        submitted = self.submit("local-refresh")

        workspace_preparer.assert_called_once_with(
            submitted["task_id"], "omcp/task-11111111",
            self.config.profile("default"), True,
        )

    def test_owner_synced_local_worker_skips_fetch_and_checks_pin(self):
        sha = "a" * 40
        self.controller.config = replace(
            self.config, local_worker_git_mode="owner_synced", local_worker_source_sha=sha,
        )
        profile = self.controller.config.profile("default")
        def fake_git(_root, *args, **_kwargs):
            return (1, "") if "show-ref" in args else (0, sha)
        with patch.object(self.controller, "_verify_profile_origin"), \
                patch.object(self.controller, "_git", side_effect=fake_git) as git:
            result = self.controller._prepare_workspace("test-owner", "omcp/task-owner", profile, False)
        self.assertEqual(result, self.state / "worktrees" / "default" / "test-owner")
        self.assertFalse(any("fetch" in call.args for call in git.call_args_list))
        self.assertTrue(any("rev-parse" in call.args for call in git.call_args_list))

    def test_owner_synced_local_worker_rejects_mismatched_ref(self):
        self.controller.config = replace(
            self.config, local_worker_git_mode="owner_synced", local_worker_source_sha="a" * 40,
        )
        with patch.object(self.controller, "_verify_profile_origin"), \
                patch.object(self.controller, "_git", return_value=(0, "b" * 40)):
            with self.assertRaisesRegex(RuntimeError, "differs from pinned"):
                self.controller._prepare_workspace(
                    "test-mismatch", "omcp/task-mismatch",
                    self.controller.config.profile("default"), False,
                )

    def test_profiles_prepare_unrelated_repositories_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root_a = base / "repo-a"; root_a.mkdir()
            root_b = base / "repo-b"; root_b.mkdir()
            profiles = {
                "a": RepositoryProfile(
                    "a", "owner/repo-a", root_a, "origin/main",
                    "owner_synced", "a" * 40,
                ),
                "b": RepositoryProfile(
                    "b", "owner/repo-b", root_b, "origin/main", "fetch", None,
                ),
            }
            self.controller.config = replace(
                self.config, repository_profiles=profiles, default_profile="a",
                local_worker_git_mode="owner_synced",
                local_worker_source_sha="a" * 40,
            )
            calls = []

            def fake_git(root, *args, **_kwargs):
                calls.append((Path(root).name, args))
                if args[:2] == ("remote", "get-url"):
                    repo = "repo-a" if Path(root).name == "repo-a" else "repo-b"
                    return 0, f"https://github.com/owner/{repo}.git"
                if args[0] == "fetch":
                    return 0, ""
                if args[:2] == ("rev-parse", "--verify"):
                    return 0, "a" * 40
                if args[0] == "show-ref":
                    return 1, ""
                return 0, ""

            with patch.object(self.controller, "_git", side_effect=fake_git):
                path_a = self.controller._prepare_workspace(
                    "task-a", "omcp/task-a", self.controller.config.profile("a"), False,
                )
                calls_a = list(calls)
                calls.clear()
                path_b = self.controller._prepare_workspace(
                    "task-b", "omcp/task-b", self.controller.config.profile("b"), True,
                )
                calls_b = list(calls)
        self.assertEqual(path_a, self.state / "worktrees" / "a" / "task-a")
        self.assertEqual(path_b, self.state / "worktrees" / "b" / "task-b")
        self.assertFalse(any(args[0] == "fetch" for _root, args in calls_a))
        self.assertTrue(any(args[0] == "fetch" for _root, args in calls_b))
        self.assertTrue(any(
            args[:2] == ("rev-parse", "--verify") for _root, args in calls_a
        ))
        self.assertFalse(any(
            args[:2] == ("rev-parse", "--verify") for _root, args in calls_b
        ))

    def test_owner_synced_local_worker_rejects_wrong_origin(self):
        self.controller.config = replace(
            self.config, local_worker_git_mode="owner_synced", local_worker_source_sha="a" * 40,
        )
        with patch.object(self.controller, "_git", return_value=(0, "https://github.com/other/repo.git")):
            with self.assertRaisesRegex(RuntimeError, "origin must match"):
                self.controller._prepare_workspace(
                    "test-origin", "omcp/task-origin",
                    self.controller.config.profile("default"), False,
                )

    def test_systemd_user_worker_command_is_hardened(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_dir = root / "task"
            workspace = root / "workspace"
            git_dir = root / "git-worktree"
            codex_home = root / "codex-home"
            for path in (task_dir, workspace, git_dir, codex_home):
                path.mkdir()
            environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(root)}
            with patch("omcp.controller.host_platform", return_value="linux"), \
                    patch("omcp.controller.shutil.which",
                          side_effect=lambda name: f"/usr/bin/{name}"), \
                    patch("omcp.controller.systemd_user_runtime_dir", return_value=root):
                command, unit, runtime = systemd_user_worker_command(
                    ["/usr/bin/python3", "-m", "omcp.runner", "--spec", "spec.json"],
                    task_id="11111111-1111-1111-1111-111111111111",
                    task_dir=task_dir, workspace=workspace,
                    git_metadata_paths=(git_dir,), codex_home=codex_home,
                    source_root=root, max_worker_seconds=90,
                    environment=environment,
                )
        self.assertEqual(command[0], "/usr/bin/systemd-run")
        self.assertIn("--user", command)
        self.assertIn("--wait", command)
        self.assertIn("--collect", command)
        self.assertIn("--unit=orchestra-worker-11111111-1111-1111-1111-111111111111", command)
        properties = {value.removeprefix("--property=") for value in command
                      if value.startswith("--property=")}
        self.assertIn("ProtectSystem=strict", properties)
        self.assertIn("ProtectHome=read-only", properties)
        self.assertIn("PrivateTmp=yes", properties)
        self.assertIn("NoNewPrivileges=yes", properties)
        self.assertIn("RestrictSUIDSGID=no", properties)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK", properties)
        self.assertIn(f"ReadWritePaths={task_dir}", properties)
        self.assertIn(f"ReadWritePaths={workspace}", properties)
        self.assertIn(f"ReadWritePaths={git_dir}", properties)
        self.assertIn(f"ReadWritePaths={codex_home}", properties)
        self.assertEqual(unit, "orchestra-worker-11111111-1111-1111-1111-111111111111")
        self.assertEqual(runtime, root)
        self.assertEqual(environment["XDG_RUNTIME_DIR"], str(root))
        self.assertIn("--", command)
        self.assertEqual(command[command.index("--") + 1:], [
            "/usr/bin/python3", "-m", "omcp.runner", "--spec", "spec.json",
        ])

    def test_systemd_user_worker_command_rejects_non_linux_hosts(self):
        with patch("omcp.controller.host_platform", return_value="darwin"):
            with self.assertRaisesRegex(RuntimeError, "only on Linux"):
                systemd_user_worker_command(
                    ["/usr/bin/python3", "-m", "omcp.runner"],
                    task_id="11111111-1111-1111-1111-111111111111",
                    task_dir=Path("/tmp/task"), workspace=Path("/tmp/workspace"),
                    git_metadata_paths=(), codex_home=None, source_root=Path("/tmp"),
                    max_worker_seconds=90, environment={},
                )

    def test_systemd_user_launcher_is_used_for_local_codex_worker(self):
        self.controller.config = replace(
            self.config, local_worker_launcher="systemd-user",
        )
        unit = "orchestra-worker-11111111-1111-1111-1111-111111111111"
        launch = Mock(return_value=(
            ["/usr/bin/systemd-run", "--user", "--unit=" + unit], unit, self.root,
        ))
        with patch("omcp.controller.systemd_user_worker_command", launch):
            submitted = self.submit("systemd-launch")
        self.assertEqual(submitted["status"], "running")
        self.assertEqual(submitted["worker_unit"], unit)
        self.assertEqual(self.popen.call_args.args[0][0], "/usr/bin/systemd-run")
        launch.assert_called_once()

    def _deepseek_controller(self, **overrides):
        self.controller.close()
        credential = Path(self.temp.name) / "deepseek-key"
        credential.write_text("test-key-not-used\n")
        os.chmod(credential, 0o600)
        config = Config(
            "owner/repo", self.root, self.state, "origin/main",
            {"deepseek": Worker(
                "deepseek", (), adapter="deepseek", provider="deepseek",
                model="deepseek-flash",
                credential_file=str(credential), endpoint_host="api.deepseek.com",
            )},
            Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
            **overrides,
        )
        self.controller = TaskController(
            config, popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )
        submitted = self.controller.submit_task(
            "deepseek-request", "DeepSeek task", "Respond without editing",
            "deepseek", True, None, "routine_development",
        )
        self.controller.resolve_approval(
            submitted["task_id"], "approve", "approved",
            approver_identity="local:test-operator",
        )
        return submitted["task_id"]

    def test_systemd_user_launcher_is_used_for_direct_deepseek_worker(self):
        unit = "orchestra-worker-11111111-1111-1111-1111-111111111111"
        launch = Mock(return_value=(
            ["/usr/bin/systemd-run", "--user", "--unit=" + unit], unit, self.root,
        ))
        with patch("omcp.controller.deepseek_runner_command",
                   return_value=["/usr/bin/bwrap", "--", "/usr/bin/true"]), \
                patch("omcp.controller.systemd_user_worker_command", launch):
            self._deepseek_controller(local_worker_launcher="systemd-user")
        launch.assert_called_once()
        self.assertEqual(self.popen.call_args.args[0][0], "/usr/bin/systemd-run")
        task = next(iter(self.controller.state["tasks"].values()))
        self.assertEqual(task["worker_unit"], unit)

    def test_immediate_deepseek_child_exit_retains_exit_code_and_category(self):
        self.controller.close()
        credential = Path(self.temp.name) / "deepseek-key"
        credential.write_text("test-key-not-used\n")
        os.chmod(credential, 0o600)
        self.controller = TaskController(
            Config(
                "owner/repo", self.root, self.state, "origin/main",
                {"deepseek": Worker(
                    "deepseek", (), adapter="deepseek", provider="deepseek",
                    model="deepseek-flash",
                    credential_file=str(credential), endpoint_host="api.deepseek.com",
                )},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
            ),
            popen=subprocess.Popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )
        command = [
            "/bin/sh", "-c",
            "echo 'bwrap: No permissions to create new namespace' >&2; exit 7",
        ]
        with patch("omcp.controller.deepseek_runner_command", return_value=command):
            submitted = self.controller.submit_task(
                "immediate", "Immediate", "Fail before the lease",
                "deepseek", True, None, "routine_development",
            )
            task = self.controller.resolve_approval(
                submitted["task_id"], "approve", "approved",
                approver_identity="local:test-operator",
            )
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["exit_code"], 7)
        self.assertEqual(task["failure"], {
            "type": "runner_startup_failed", "category": "sandbox_namespace_denied",
            "exit_code": 7,
        })
        self.assertEqual(task["reservation"]["status"], "released")
        self.assertFalse(self.controller._children)
        task_dir = self.state / "tasks" / task["task_id"]
        self.assertIn("No permissions", (task_dir / "startup.log").read_text())
        persisted = (self.state / "state.json").read_text()
        self.assertNotIn("No permissions", persisted)
        self.assertNotIn("bwrap:", persisted)

    def test_disappeared_runner_keeps_a_bounded_startup_category(self):
        submitted = self.submit("disappeared")
        task = self.controller.state["tasks"][submitted["task_id"]]
        task["runner_pid"] = 999999
        task_dir = self.state / "tasks" / submitted["task_id"]
        task["startup_log_path"] = str(task_dir / "startup.log")
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "startup.log").write_text(
            "bwrap: Failed to unshare user namespace\n",
        )
        self.controller.reconcile()
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["failure"], {
            "type": "runner_startup_failed", "category": "sandbox_namespace_denied",
        })
        self.assertEqual(task["reservation"]["status"], "released")

    def test_bridge_status_identifies_the_durable_state_store_without_a_path(self):
        status = self.controller.bridge_status()
        self.assertRegex(status["state_store_id"], r"^[0-9a-f]{24}$")
        self.assertNotIn("state_dir", status)
        self.assertEqual(status["coordinator_pid"], os.getpid())
        self.assertIsInstance(status["coordinator_started_at"], str)

    def test_cancelling_transient_worker_stops_its_unit(self):
        self.controller.config = replace(
            self.config, local_worker_launcher="systemd-user",
        )
        unit = "orchestra-worker-11111111-1111-1111-1111-111111111111"
        launch = Mock(return_value=(
            ["/usr/bin/systemd-run", "--user", "--unit=" + unit], unit, self.root,
        ))
        with patch("omcp.controller.systemd_user_worker_command", launch):
            submitted = self.submit("systemd-cancel")
        task = self.controller.state["tasks"][submitted["task_id"]]
        task["runner_pid"] = 999999
        with patch("omcp.controller.shutil.which", return_value="/usr/bin/systemctl"), \
                patch.object(Path, "is_socket", return_value=True), \
                patch("omcp.controller.subprocess.run") as stop:
            self.controller.cancel_task(submitted["task_id"], "test cancellation")
        stop.assert_any_call(
            ["/usr/bin/systemctl", "--user", "stop", unit],
            env=ANY, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=10,
        )

    def test_opencode_failure_exposes_only_allowlisted_diagnostic_fields(self):
        self.controller.close()
        self.controller = TaskController(
            Config(
                "owner/repo", self.root, self.state, "origin/main",
                {"opencode": Worker(
                    "opencode", ("/allowlisted/opencode-cli",), adapter="opencode",
                    provider="deepseek", model="deepseek/deepseek-chat",
                    credential_file="/private/key", endpoint_host="api.deepseek.com",
                )},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )
        submitted = self.controller.submit_task(
            "opencode-redacted", "Harmless", "Reply without tools", "opencode", True,
        )
        self.controller.resolve_approval(
            submitted["task_id"], "approve", "Approved harmless provider check",
            approver_identity="local:test-operator",
        )
        task = self.controller.state["tasks"][submitted["task_id"]]
        Path(task["result_path"]).write_text("generic result")
        Path(task["completion_path"]).write_text(json.dumps({
            "exit_code": 1, "finished_at": "2026-10-11T00:00:00+00:00",
            "runner_error": "OpenCodeExecutionFailed",
            "worker_diagnostic": {
                "category": "authentication", "status": 401, "exit_cause": "process_exit",
            },
        }))

        status = self.controller.task_status(submitted["task_id"])

        self.assertEqual(status["failure"], {
            "type": "OpenCodeExecutionFailed", "category": "authentication",
            "status": 401, "exit_cause": "process_exit",
        })
        self.assertNotIn("prompt", json.dumps(status["failure"]))
        self.assertNotIn("credential", json.dumps(status["failure"]))

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
            "writable_files": [],
            "network": False, "external_credentials": False, "git_metadata": "read_only",
        })
        self.assertEqual(self.controller.list_approvals()["approvals"], [])
        self.popen.assert_called_once()
        self.assertTrue((self.root / "src").is_dir())
        self.assertTrue((self.root / "tests").is_dir())
        prompt = (self.state / "tasks" / submitted["task_id"] / "prompt.txt").read_text()
        self.assertIn("ORCHESTRA STRUCTURED COMPLETION CONTRACT", prompt)
        self.assertNotIn("orchestra-security-probe", prompt)
        audit = (self.state / "audit.jsonl").read_text()
        self.assertIn('"authorization_mode":"standing"', audit)
        self.assertNotIn("Change local code and run tests", audit)

    def test_exact_file_grant_creates_parents_and_preserves_completion_write(self):
        self.controller.close()
        self.controller = TaskController(
            Config(
                "owner/repo", self.root, self.state, "origin/main",
                {"ina": Worker("ina", ("codex",))},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                standing_authorizations=(StandingAuthorization(
                    "ina-orchestra-dashboard-v1", "routine_development",
                    frozenset({"default"}), frozenset({"ina"}), (),
                    (
                        "dashboard/app.py", "dashboard/system.py",
                        "dashboard/static/system.js", "dashboard/README.md",
                    ),
                ),),
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )

        submitted = self.controller.submit_task(
            "dashboard-files", "Update dashboard", "Edit the reviewed dashboard files",
            "ina", True, "default", "routine_development",
        )

        self.assertEqual(submitted["status"], "running")
        self.assertEqual(submitted["execution_capabilities"]["writable_paths"], [])
        self.assertEqual(submitted["execution_capabilities"]["writable_files"], [
            "dashboard/app.py", "dashboard/system.py",
            "dashboard/static/system.js", "dashboard/README.md",
        ])
        self.assertTrue((self.root / "dashboard").is_dir())
        self.assertTrue((self.root / "dashboard/static").is_dir())
        self.assertFalse((self.root / "dashboard/app.py").exists())
        self.assertFalse((self.root / "unrelated").exists())
        task = self.controller.state["tasks"][submitted["task_id"]]
        self.assertEqual(task["routine_writable_paths"], [])
        self.assertEqual(task["routine_writable_files"], [
            "dashboard/app.py", "dashboard/system.py",
            "dashboard/static/system.js", "dashboard/README.md",
        ])
        spec = json.loads(
            (self.state / "tasks" / submitted["task_id"] / "runner.json").read_text()
        )
        self.assertEqual(spec["execution_capabilities"]["writable_files"], task["routine_writable_files"])
        audit = (self.state / "audit.jsonl").read_text()
        self.assertIn("ina-orchestra-dashboard-v1", audit)
        self.assertNotIn("Edit the reviewed dashboard files", audit)

    def test_file_grants_are_strictly_validated(self):
        # Configuration loading performs the authoritative path validation.
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
                    "id": "ina-orchestra-dashboard-v1",
                    "consequence_class": "routine_development",
                    "repository_profiles": ["default"],
                    "workers": ["ina"],
                    "writable_paths": [],
                    "writable_files": ["dashboard/system.py"],
                }],
            }
            config_path.write_text(json.dumps(raw))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                loaded = Config.load(config_path)
            self.assertEqual(loaded.standing_authorizations[0].writable_files, ("dashboard/system.py",))
            for bad in (
                ["../outside.py"], ["/etc/passwd"], [".git/config"],
                ["dashboard/../outside.py"], ["dashboard/system.py", "dashboard/system.py"],
            ):
                raw["standing_authorizations"][0]["writable_files"] = bad
                config_path.write_text(json.dumps(raw))
                with patch("omcp.controller.shutil.which", return_value=str(codex)):
                    with self.assertRaisesRegex(ValueError, "writable_files"):
                        Config.load(config_path)

    def test_standing_authorizations_must_not_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            archive = Path(directory) / "archive"
            archive.mkdir()
            codex = Path(directory) / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o700)
            config_path = Path(directory) / "omcp.json"
            raw = {
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(Path(directory) / "state"),
                "workers": {"ina": {"command": [str(codex)]}},
                "principal": {"identity": "chatgpt:test", "permissions": ["tasks:submit"]},
                "repository_profiles": {
                    "orchestra": {
                        "repository": "owner/repo", "project_root": str(root),
                        "base_ref": "origin/main",
                    },
                    "orchestra-dashboard": {
                        "repository": "owner/repo", "project_root": str(root),
                        "base_ref": "origin/main",
                    },
                },
                "default_profile": "orchestra",
                "standing_authorizations": [
                    {
                        "id": "tests", "consequence_class": "routine_development",
                        "repository_profiles": ["orchestra"], "workers": ["ina"],
                        "writable_paths": ["tests"],
                    },
                    {
                        "id": "dashboard", "consequence_class": "routine_development",
                        "repository_profiles": ["orchestra-dashboard"],
                        "workers": ["ina"], "writable_paths": [],
                        "writable_files": ["dashboard/system.py"],
                    },
                ],
            }
            config_path.write_text(json.dumps(raw))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                loaded = Config.load(config_path)
            self.assertEqual(
                {grant.authorization_id for grant in loaded.standing_authorizations},
                {"tests", "dashboard"},
            )
            raw["standing_authorizations"][1]["repository_profiles"] = ["orchestra"]
            config_path.write_text(json.dumps(raw))
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                with self.assertRaisesRegex(ValueError, "overlap"):
                    Config.load(config_path)

    def test_dashboard_profile_grant_is_selected_without_touching_tests_grant(self):
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
                "repository_profiles": {
                    "orchestra": {
                        "repository": "owner/repo", "project_root": str(root),
                        "base_ref": "origin/main",
                    },
                    "orchestra-dashboard": {
                        "repository": "owner/repo", "project_root": str(root),
                        "base_ref": "origin/main",
                    },
                },
                "default_profile": "orchestra",
                "standing_authorizations": [
                    {
                        "id": "tests", "consequence_class": "routine_development",
                        "repository_profiles": ["orchestra"], "workers": ["ina"],
                        "writable_paths": ["tests"],
                    },
                    {
                        "id": "dashboard", "consequence_class": "routine_development",
                        "repository_profiles": ["orchestra-dashboard"],
                        "workers": ["ina"], "writable_paths": [],
                        "writable_files": ["dashboard/system.py"],
                    },
                ],
            }
            config_path.write_text(json.dumps(raw))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                config = Config.load(config_path)
            controller = TaskController(
                config, popen=self.popen, workspace_preparer=lambda *_: self.root,
                id_factory=lambda: "11111111-1111-1111-1111-111111111111",
            )
            tests_task = controller.submit_task(
                "tests-task", "Tests only", "Edit tests", "ina", True, "orchestra",
                "routine_development",
            )
            self.assertEqual(tests_task["authorization"]["authorization_id"], "tests")
            self.assertEqual(tests_task["execution_capabilities"]["writable_paths"], ["tests"])
            dashboard_task = controller.submit_task(
                "dashboard-task", "Dashboard only", "Edit dashboard", "ina", True,
                "orchestra-dashboard", "routine_development",
            )
            self.assertEqual(
                dashboard_task["authorization"]["authorization_id"], "dashboard",
            )
            self.assertEqual(
                dashboard_task["execution_capabilities"]["writable_files"],
                ["dashboard/system.py"],
            )
            self.assertEqual(
                dashboard_task["execution_capabilities"]["writable_paths"], [],
            )
            controller.close()

    def test_deepseek_worker_preserves_approval_idempotency_and_emits_no_credential_value(self):
        credential = Path(self.temp.name) / "deepseek.key"
        credential.write_text("never-log-this")
        credential.chmod(0o600)
        self.controller.close()
        self.controller = TaskController(
            Config(
                "owner/repo", self.root, self.state, "origin/main",
                {"deepseek": Worker(
                    "deepseek", (), adapter="deepseek",
                    provider="deepseek", model="deepseek-flash",
                    credential_file=str(credential),
                )},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                max_worker_seconds=600,
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )
        submitted = self.controller.submit_task(
            "deepseek-request", "Inspect code", "Make no network changes", "deepseek", True,
        )
        self.assertEqual(submitted["status"], "approval_required")
        replay = self.controller.submit_task(
            "deepseek-request", "Inspect code", "Make no network changes", "deepseek", True,
        )
        self.assertTrue(replay["idempotent_replay"])
        self.controller.resolve_approval(
            submitted["task_id"], "approve", "Local operator approved",
            approver_identity="local:test-operator",
        )
        spec = json.loads((self.state / "tasks" / submitted["task_id"] / "runner.json").read_text())
        self.assertEqual(spec["adapter"], "deepseek")
        self.assertEqual(spec["provider"], "deepseek")
        self.assertEqual(spec["model"], "deepseek-flash")
        self.assertEqual(spec["command"], [])
        self.assertEqual(spec["endpoint_host"], "api.deepseek.com")
        self.assertEqual(spec["max_worker_seconds"], 600)
        self.assertEqual(spec["credential_file"], str(credential))
        self.assertNotIn("never-log-this", (self.state / "audit.jsonl").read_text())

    def test_deepseek_runs_without_human_approval_when_global_approvals_are_disabled(self):
        credential = Path(self.temp.name) / "deepseek.key"
        credential.write_text("not-printed")
        credential.chmod(0o600)
        self.controller.close()
        self.controller = TaskController(
            Config("owner/repo", self.root, self.state, "origin/main", {
                "deepseek": Worker("deepseek", (), adapter="deepseek", provider="deepseek",
                                   model="deepseek-flash", credential_file=str(credential)),
            }, Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
            task_approval_mode="disabled"), popen=self.popen,
            workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111")
        task = self.controller.submit_task(
            "direct-approval", "Review", "Do not edit", "deepseek",
            consequence_class="routine_development",
        )
        self.assertNotEqual(task["status"], "approval_required")
        self.assertEqual(task["approval"]["status"], "not_required")

    def test_omitted_consequence_class_is_rejected_without_human_approval(self):
        self.controller.close()
        self.controller = TaskController(
            Config(
                "owner/repo", self.root, self.state, "origin/main",
                {"ina": Worker("ina", ("codex",))},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                task_approval_mode="disabled",
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )
        with self.assertRaisesRegex(ValueError, "rejected without human approval"):
            self.controller.submit_task("unclassified-default", "Review", "Do not edit", "ina")
        self.assertEqual(self.popen.call_count, 0)

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

    def test_disabled_approval_policy_rejects_non_routine_work_without_waiting(self):
        self.controller.close()
        self.controller = TaskController(
            Config(
                "owner/repo", self.root, self.state, "origin/main",
                {"ina": Worker("ina", ("codex",))},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                task_approval_mode="disabled",
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
        )

        for request_id, consequence_class in (
            ("automatic-security", "security_change"),
            ("automatic-unclassified", "unclassified"),
        ):
            with self.assertRaisesRegex(ValueError, "rejected without human approval"):
                self.controller.submit_task(
                    request_id, "Consequential work", "Perform bounded work", "ina",
                    True, "default", consequence_class,
                )
        self.assertEqual(self.controller.list_approvals()["approvals"], [])
        self.popen.assert_not_called()
        audit = (self.state / "audit.jsonl").read_text()
        self.assertIn("autonomous_mode_requires_routine_development", audit)

    def test_disabled_approval_policy_runs_routine_coding_without_manual_approval(self):
        self.controller.close()
        self.controller = TaskController(
            Config(
                "owner/repo", self.root, self.state, "origin/main",
                {"ina": Worker("ina", ("codex",))},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                task_approval_mode="disabled",
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: "11111111-1111-1111-1111-111111111111",
        )
        submitted = self.controller.submit_task(
            "routine-coding", "Adjust CSS", "Update the stylesheet and tests", "ina",
            True, "default", "routine_development",
        )
        self.assertEqual(submitted["status"], "running")
        self.assertEqual(submitted["authorization"]["mode"], "automatic")
        self.assertEqual(submitted["approval"]["status"], "not_required")
        self.assertEqual(self.controller.list_approvals()["approvals"], [])
        self.popen.assert_called_once()
        self.complete(submitted["task_id"], result="routine task completed")
        self.controller.reconcile()
        self.assertEqual(self.controller.task_status(submitted["task_id"])["status"], "succeeded")
        self.assertEqual(
            self.controller.task_result(submitted["task_id"])["result"],
            "routine task completed",
        )

    def test_disabled_approval_policy_releases_existing_pending_request(self):
        submitted = self.controller.submit_task(
            "pending-before-policy-change", "Queued work", "Perform bounded work", "ina",
            True, None, "routine_development",
        )
        self.assertEqual(submitted["status"], "approval_required")
        self.controller.close()
        self.controller = TaskController(
            Config(
                "owner/repo", self.root, self.state, "origin/main",
                {"ina": Worker("ina", ("codex",))},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                task_approval_mode="disabled",
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
        )

        self.controller.tick()

        task = self.controller.task_status(submitted["task_id"])
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["approval"]["status"], "not_required")
        self.assertEqual(task["authorization"]["mode"], "automatic")
        self.assertIn('"action":"approval.bypass"', (self.state / "audit.jsonl").read_text())

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

    def test_codex_spec_enables_the_sandbox_startup_probe(self):
        submitted = self.submit("probe-spec")
        spec_path = self.state / "tasks" / submitted["task_id"] / "runner.json"
        spec = json.loads(spec_path.read_text())
        self.assertIs(spec["sandbox_startup_probe"], True)

    def test_exit_zero_without_required_result_is_reconciled_as_failed(self):
        submitted = self.submit("missing-result")
        task = self.controller.state["tasks"][submitted["task_id"]]
        Path(task["completion_path"]).write_text(json.dumps({
            "exit_code": 0, "finished_at": "2026-10-11T00:00:00+00:00",
            "runner_error": None,
        }))
        self.controller.reconcile()
        reconciled = self.controller.state["tasks"][submitted["task_id"]]
        self.assertEqual(reconciled["status"], "failed")
        self.assertEqual(reconciled["exit_code"], 125)
        self.assertEqual(reconciled["failure"], {"type": "MissingWorkerResult"})

    def test_exit_zero_without_completion_contract_is_failed(self):
        submitted = self.submit("missing-contract")
        task = self.controller.state["tasks"][submitted["task_id"]]
        Path(task["result_path"]).write_text("required operation was denied\n")
        Path(task["completion_path"]).write_text(json.dumps({
            "exit_code": 0, "finished_at": "2026-10-11T00:00:00+00:00",
            "runner_error": None,
        }))
        self.controller.reconcile()
        reconciled = self.controller.state["tasks"][submitted["task_id"]]
        self.assertEqual(reconciled["status"], "failed")
        self.assertEqual(reconciled["failure"], {"type": "TaskContractFailed"})

    def _standing_completion(self, request_id: str):
        submitted = self.submit(request_id)
        task = self.controller.state["tasks"][submitted["task_id"]]
        task["requires_security_assertions"] = True
        (self.root / ".orchestra-tmp").mkdir(exist_ok=True)
        Path(task["result_path"]).write_text("worker diagnostic\n")
        Path(task["completion_path"]).write_text(json.dumps({
            "exit_code": 0, "finished_at": "2026-10-11T00:00:00+00:00",
            "runner_error": None, "completion_contract": "success",
        }))
        return submitted, task

    @staticmethod
    def _trusted_checks():
        return {
            name: {
                "name": name, "attempted": True, "allowed": False,
                "error_type": "PermissionError", "command": "probe " + name,
            }
            for name in worker_contract.REQUIRED_SECURITY_CHECKS
        }

    def test_worker_cannot_forge_trusted_security_evidence(self):
        actions = ("replace-probe", "delete-probe", "fabricate-report", "edit-report", "skip-probe")
        for action in actions:
            with self.subTest(action=action):
                self.controller.close()
                self.controller = TaskController(
                    self.config, popen=self.popen,
                    workspace_preparer=lambda *_: self.root,
                    id_factory=lambda: str(uuid.uuid4()),
                )
                contract_dir = self.root / ".orchestra-tmp"
                if contract_dir.exists():
                    for child in contract_dir.iterdir():
                        if child.is_file():
                            child.unlink()
                submitted, task = self._standing_completion("adversarial-" + action)
                probe = self.root / ".orchestra-tmp/orchestra-security-probe.py"
                report = self.root / ".orchestra-tmp/orchestra-security-probe.json"
                if action == "replace-probe":
                    probe.write_text("fake probe\n")
                elif action == "delete-probe":
                    probe.unlink(missing_ok=True)
                elif action == "fabricate-report":
                    report.write_text(json.dumps({"schema_version": 1, "checks": []}))
                elif action == "edit-report":
                    report.write_text(json.dumps({"schema_version": 1, "checks": []}))
                    report.write_text(json.dumps({"schema_version": 1, "checks": []}) + " ")
                with patch(
                    "omcp.controller.trusted_probe.run_trusted_security_probe",
                    side_effect=RuntimeError("trusted probe failed"),
                ) as verify:
                    self.controller.reconcile()
                verify.assert_called_once()
                reconciled = self.controller.state["tasks"][submitted["task_id"]]
                self.assertEqual(reconciled["status"], "failed")
                self.assertEqual(reconciled["failure"], {"type": "SecurityAssertionFailed"})
                self.assertEqual(reconciled["security_assertions"], "failed")

    def test_trusted_security_evidence_success_marks_task_succeeded(self):
        submitted, task = self._standing_completion("trusted-success")
        with patch(
            "omcp.controller.trusted_probe.run_trusted_security_probe",
            return_value=self._trusted_checks(),
        ) as verify:
            self.controller.reconcile()
        verify.assert_called_once()
        reconciled = self.controller.state["tasks"][submitted["task_id"]]
        self.assertEqual(reconciled["status"], "succeeded")
        self.assertEqual(reconciled["security_assertions"], "passed")
        self.assertEqual(
            {item["name"] for item in reconciled["security_checks"]},
            set(worker_contract.REQUIRED_SECURITY_CHECKS),
        )

    def test_unexpectedly_allowed_trusted_assertion_fails_task(self):
        submitted, task = self._standing_completion("trusted-allowed")
        with patch(
            "omcp.controller.trusted_probe.run_trusted_security_probe",
            side_effect=ValueError("security probe allowed read_orchestra_config"),
        ):
            self.controller.reconcile()
        reconciled = self.controller.state["tasks"][submitted["task_id"]]
        self.assertEqual(reconciled["status"], "failed")
        self.assertEqual(reconciled["failure"], {"type": "SecurityAssertionFailed"})

    def test_sandbox_startup_failure_is_visible_in_task_diagnostics(self):
        submitted = self.submit("sandbox-failure")
        task = self.controller.state["tasks"][submitted["task_id"]]
        Path(task["result_path"]).write_text("Worker sandbox failed to start.\n")
        Path(task["completion_path"]).write_text(json.dumps({
            "exit_code": 125, "finished_at": "2026-10-11T00:00:00+00:00",
            "runner_error": "SandboxStartupError",
        }))
        self.controller.reconcile()
        reconciled = self.controller.state["tasks"][submitted["task_id"]]
        self.assertEqual(reconciled["status"], "failed")
        self.assertEqual(reconciled["failure"], {"type": "SandboxStartupError"})
        result = self.controller.task_result(submitted["task_id"])
        self.assertEqual(result["result"], "Worker sandbox failed to start.\n")

    def test_cancelled_reaped_runner_without_completion_is_terminal(self):
        submitted = self.submit()
        task_id = submitted["task_id"]
        self.controller.state["tasks"][task_id]["status"] = "cancel_requested"
        with patch("omcp.controller.os.waitpid", return_value=(FakeProcess.pid, signal.SIGTERM)):
            self.controller.reconcile()
        reconciled = self.controller.state["tasks"][task_id]
        self.assertEqual(reconciled["status"], "cancelled")
        self.assertEqual(reconciled["exit_code"], -signal.SIGTERM)
        self.assertIsNotNone(reconciled["finished_at"])

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

    def test_result_reader_does_not_read_past_bound_sentinel(self):
        self.controller.config = Config(
            self.config.repository, self.config.project_root, self.config.state_dir,
            self.config.base_ref, self.config.workers, self.config.principal,
            max_result_bytes=1024,
        )
        submitted = self.submit()
        self.complete(submitted["task_id"], result="x" * 4096)
        with patch("pathlib.Path.read_bytes", side_effect=AssertionError("unbounded read")):
            result = self.controller.task_result(submitted["task_id"])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["result"]), 1024)

    def test_host_operation_is_exact_commit_local_approval_only(self):
        target = "1" * 40
        harness = "2" * 40
        self.controller.config = Config(
            self.config.repository, self.config.project_root, self.config.state_dir,
            self.config.base_ref, self.config.workers, self.config.principal,
            host_operations={
                "delnavazan-mariadb": HostOperation(
                    "delnavazan-mariadb", "delnavazan_mariadb_acceptance_v1",
                    "default", harness, 1800,
                ),
            },
        )
        self.controller.host_workspace_preparer = lambda *_: self.root
        with patch.object(self.controller, "_resolve_local_commit", side_effect=lambda _p, value: value):
            submitted = self.controller.submit_host_operation(
                "host-request-1", "delnavazan-mariadb", target,
            )
            self.assertEqual(submitted["status"], "approval_required")
            self.assertEqual(submitted["target_commit"], target)
            self.assertEqual(submitted["harness_commit"], harness)
            self.popen.assert_not_called()
            details = self.controller.local_approval_details(submitted["task_id"])
            review = format_review(details, "approve")
            self.assertIn("Operation:    delnavazan-mariadb", review)
            self.assertIn("Harness SHA:  " + harness, review)
            self.assertIn("Target SHA:   " + target, review)
            with patch.dict(os.environ, {
                "OPENAI_API_KEY": "must-not-pass",
                "OMCP_AUTHENTICATED_CALLER": "must-not-pass",
            }):
                approved = self.controller.resolve_approval(
                    submitted["task_id"], "approve", "Exact bounded harness approved",
                    "local:test-operator",
                )
        self.assertEqual(approved["status"], "running")
        self.assertEqual(approved["resource_requirements"], {
            "worker": "host:delnavazan-mariadb",
            "repository": "owner/repo",
            "access": "write",
        })
        self.assertEqual(approved["reservation"]["status"], "active")
        command = self.popen.call_args.args[0]
        self.assertIn("omcp.host_runner", command)
        runner_environment = self.popen.call_args.kwargs["env"]
        self.assertNotIn("OPENAI_API_KEY", runner_environment)
        self.assertNotIn("OMCP_AUTHENTICATED_CALLER", runner_environment)
        spec = json.loads((self.state / "tasks" / submitted["task_id"] / "host-operation.json").read_text())
        self.assertEqual(spec["target_commit"], target)
        self.assertEqual(spec["harness_commit"], harness)
        self.assertEqual(spec["task_id"], submitted["task_id"])
        self.assertEqual(spec["lease_id"], self.controller.state["tasks"][
            submitted["task_id"]
        ]["startup_lease"]["lease_id"])
        self.assertNotIn("command", spec)

        task = self.controller.state["tasks"][submitted["task_id"]]
        task.pop("runner_pid")
        task["status"] = "starting"
        self.complete(submitted["task_id"])
        self.controller.reconcile()
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(task["reservation"]["status"], "released")

    def test_host_operation_rejects_caller_commands_and_unknown_ids(self):
        with self.assertRaisesRegex(ValueError, "unknown or disabled"):
            self.controller.submit_host_operation("host-request", "arbitrary-shell", "1" * 40)
        self.assertEqual(self.controller.state["tasks"], {})

    def test_host_operation_startup_failure_releases_scheduler_reservation(self):
        target = "1" * 40
        harness = "2" * 40
        self.controller.config = Config(
            self.config.repository, self.config.project_root, self.config.state_dir,
            self.config.base_ref, self.config.workers, self.config.principal,
            host_operations={
                "delnavazan-mariadb": HostOperation(
                    "delnavazan-mariadb", "delnavazan_mariadb_acceptance_v1",
                    "default", harness, 1800,
                ),
            },
        )
        self.controller.host_workspace_preparer = Mock(side_effect=RuntimeError("unavailable"))
        with patch.object(self.controller, "_resolve_local_commit", side_effect=lambda _p, value: value):
            submitted = self.controller.submit_host_operation(
                "host-startup-failure", "delnavazan-mariadb", target,
            )
            failed = self.controller.resolve_approval(
                submitted["task_id"], "approve", "Exact bounded harness approved",
                "local:test-operator",
            )

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failure"]["stage"], "startup")
        self.assertEqual(failed["reservation"]["status"], "released")
        self.popen.assert_not_called()

    def test_host_operation_is_rejected_without_human_approval_when_task_gate_is_disabled(self):
        target = "1" * 40
        harness = "2" * 40
        self.controller.config = Config(
            self.config.repository, self.config.project_root, self.config.state_dir,
            self.config.base_ref, self.config.workers, self.config.principal,
            host_operations={
                "delnavazan-mariadb": HostOperation(
                    "delnavazan-mariadb", "delnavazan_mariadb_acceptance_v1",
                    "default", harness, 1800,
                ),
            },
            task_approval_mode="disabled",
        )
        with self.assertRaisesRegex(ValueError, "host operations are unavailable"):
            self.controller.submit_host_operation(
                "host-request-disabled", "delnavazan-mariadb", target,
            )

        self.assertEqual(self.controller.list_approvals()["approvals"], [])
        self.popen.assert_not_called()
        self.assertIn(
            "host_operations_unavailable_in_autonomous_mode",
            (self.state / "audit.jsonl").read_text(),
        )


    def test_parallel_claims_use_distinct_lanes_and_release_reservations(self):
        self.controller.close()
        ids = iter(f"{number:08d}-1111-1111-1111-111111111111" for number in range(1, 4))
        profiles = {}
        workers = {}
        for number in range(1, 4):
            root = Path(self.temp.name) / f"project-{number}"
            root.mkdir()
            profiles[f"repo-{number}"] = RepositoryProfile(
                f"repo-{number}", f"owner/repo-{number}", root, "origin/main",
            )
            workers[f"worker-{number}"] = Worker(f"worker-{number}", ("codex",))
        self.controller = TaskController(
            Config(
                "owner/repo-1", profiles["repo-1"].project_root, self.state, "origin/main",
                workers, Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                repository_profiles=profiles, default_profile="repo-1",
                task_approval_mode="disabled", max_parallel_tasks=4,
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: next(ids),
        )
        submitted = [
            self.controller.submit_task(
                f"parallel-{number}", "Parallel", "Run independently",
                f"worker-{number}", True, f"repo-{number}", "routine_development",
            )
            for number in range(1, 4)
        ]

        self.assertEqual([item["status"] for item in submitted], ["running"] * 3)
        reservations = [
            self.controller.state["tasks"][item["task_id"]]["reservation"]
            for item in submitted
        ]
        self.assertEqual({item["lane"] for item in reservations}, {1, 2, 3})
        self.assertTrue(all(item["status"] == "active" for item in reservations))
        self.complete(submitted[1]["task_id"])
        self.controller.tick()
        self.assertEqual(reservations[1]["status"], "released")
        self.assertEqual(self.controller.worker_availability()["execution_limit"], 4)

    def _same_repository_controller(
        self, worker_names=("one", "two", "three"), *, limit=2,
        mode="isolated_worktrees", ids=None,
    ):
        self.controller.close()
        identifiers = iter(ids or [
            f"{number:08d}-1111-1111-1111-111111111111" for number in range(1, 10)
        ])
        created: list[Path] = []

        def prepare(task_id, branch, profile, refresh):
            path = self.state / "worktrees" / profile.profile_id / task_id
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)
            return path

        self.controller = TaskController(
            Config(
                "owner/shared", self.root, self.state, "origin/main",
                {name: Worker(name, ("codex",)) for name in worker_names},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                task_approval_mode="disabled", max_parallel_tasks=limit,
                repository_parallelism=mode,
            ),
            popen=self.popen, workspace_preparer=prepare,
            id_factory=lambda: next(identifiers),
        )
        return self.controller, created

    def test_isolated_mode_runs_two_writers_and_promotes_the_third(self):
        controller, created = self._same_repository_controller(limit=2)
        submitted = [
            controller.submit_task(
                f"isolated-{number}", "Write", "Edit independently",
                worker, True, None, "routine_development",
            )
            for number, worker in enumerate(("one", "two", "three"), 1)
        ]
        self.assertEqual(
            [item["status"] for item in submitted], ["running", "running", "accepted"],
        )
        self.assertEqual(self.popen.call_count, 2)
        self.assertEqual(len(created), 2)
        running = [controller.state["tasks"][item["task_id"]] for item in submitted[:2]]
        self.assertEqual(len({task["workspace"] for task in running}), 2)
        self.assertEqual(len({task["branch"] for task in running}), 2)
        self.assertEqual(
            {task["reservation"]["lane"] for task in running}, {1, 2},
        )

        self.complete(submitted[0]["task_id"])
        controller.tick()
        self.assertEqual(
            controller.state["tasks"][submitted[2]["task_id"]]["status"], "running",
        )
        self.assertEqual(self.popen.call_count, 3)

    def test_same_worker_never_overlaps_in_isolated_mode(self):
        controller, _ = self._same_repository_controller(worker_names=("one",), limit=2)
        first = controller.submit_task(
            "same-worker-1", "Write", "Edit", "one", True, None, "routine_development",
        )
        second = controller.submit_task(
            "same-worker-2", "Write", "Edit", "one", True, None, "routine_development",
        )
        self.assertEqual(first["status"], "running")
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(self.popen.call_count, 1)

    def test_duplicate_branch_identity_fails_closed(self):
        controller, created = self._same_repository_controller(
            ids=[
                "11111111-1111-1111-1111-111111111111",
                "11111111-2222-2222-2222-222222222222",
            ],
        )
        first = controller.submit_task(
            "duplicate-1", "Write", "Edit", "one", True, None, "routine_development",
        )
        second = controller.submit_task(
            "duplicate-2", "Write", "Edit", "two", True, None, "routine_development",
        )
        self.assertEqual(first["status"], "running")
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["failure"]["type"], "DuplicateWorkspaceIdentity")
        self.assertEqual(second["reservation"]["status"], "released")
        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(len(created), 1)

    def test_partial_worktree_preparation_is_cleaned_up(self):
        controller, _ = self._same_repository_controller(worker_names=("one",))

        def partial(task_id, branch, profile, refresh):
            path = self.state / "worktrees" / profile.profile_id / task_id
            path.mkdir(parents=True, exist_ok=True)
            raise RuntimeError("partial worktree preparation failed")

        controller.workspace_preparer = partial
        submitted = controller.submit_task(
            "partial", "Write", "Edit", "one", True, None, "routine_development",
        )
        task = controller.state["tasks"][submitted["task_id"]]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["failure"]["type"], "RuntimeError")
        self.assertEqual(task["reservation"]["status"], "released")
        expected = (
            self.state / "worktrees" / "default" / submitted["task_id"]
        )
        self.assertFalse(expected.exists())

    def test_repository_setup_lock_is_released_before_worker_start(self):
        controller, _ = self._same_repository_controller(worker_names=("one",))
        events: list[str] = []

        @contextmanager
        def observed_lock(profile):
            events.append("lock-enter")
            yield
            events.append("lock-exit")

        def observed_popen(*_args, **_kwargs):
            events.append("popen")
            return FakeProcess()

        controller._repository_setup_lock = observed_lock
        controller.popen = observed_popen
        controller.submit_task(
            "lock", "Write", "Edit", "one", True, None, "routine_development",
        )
        self.assertIn("lock-enter", events)
        self.assertIn("lock-exit", events)
        self.assertIn("popen", events)
        self.assertLess(events.index("lock-exit"), events.index("popen"))

    def test_restart_preserves_isolated_queue_and_reservations(self):
        controller, _ = self._same_repository_controller()
        submitted = [
            controller.submit_task(
                f"restart-{number}", "Write", "Edit", worker, True, None,
                "routine_development",
            )
            for number, worker in enumerate(("one", "two", "three"), 1)
        ]
        self.assertEqual(
            [item["status"] for item in submitted], ["running", "running", "accepted"],
        )
        controller.close()
        self.controller = TaskController(
            controller.config, popen=self.popen,
            workspace_preparer=lambda task_id, branch, profile, refresh: (
                self.state / "worktrees" / profile.profile_id / task_id
            ),
        )
        recovered = [self.controller.state["tasks"][item["task_id"]] for item in submitted]
        self.assertEqual([item["status"] for item in recovered],
                         ["running", "running", "accepted"])
        self.assertEqual(
            [item["reservation"]["status"] for item in recovered[:2]],
            ["active", "active"],
        )
        self.complete(submitted[0]["task_id"])
        self.controller.tick()
        self.assertEqual(
            self.controller.state["tasks"][submitted[2]["task_id"]]["status"],
            "running",
        )

    def test_direct_deepseek_is_read_only_and_shares_only_in_isolated_mode(self):
        credential = Path(self.temp.name) / "deepseek-key"
        credential.write_text("test-key-not-used\n")
        os.chmod(credential, 0o600)

        def build(mode):
            self.controller.close()
            state = Path(self.temp.name) / f"deepseek-state-{mode}"
            self.controller = TaskController(
                Config(
                    "owner/shared", self.root, state, "origin/main",
                    {
                        "writer": Worker("writer", ("codex",)),
                        "responder": Worker(
                            "responder", (), adapter="deepseek", provider="deepseek",
                            model="deepseek/deepseek-chat",
                            credential_file=str(credential),
                            endpoint_host="api.deepseek.com",
                        ),
                    },
                    Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                    task_approval_mode="disabled", max_parallel_tasks=2,
                    repository_parallelism=mode,
                ),
                popen=self.popen, workspace_preparer=lambda *_: self.root,
                id_factory=iter([
                    "11111111-1111-1111-1111-111111111111",
                    "22222222-2222-2222-2222-222222222222",
                ]).__next__,
            )
            with patch("omcp.controller.deepseek_runner_command",
                       return_value=["/usr/bin/true"]):
                first = self.controller.submit_task(
                    "writer", "Write", "Edit", "writer", True, None,
                    "routine_development",
                )
                second = self.controller.submit_task(
                    "respond", "Respond", "Read only", "responder", True, None,
                    "routine_development",
                )
            return first, second

        first, second = build("serialized")
        self.assertEqual(first["status"], "running")
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(
            self.controller.state["tasks"][second["task_id"]]["resource_requirements"]["access"],
            "read",
        )

        first, second = build("isolated_worktrees")
        self.assertEqual([first["status"], second["status"]], ["running", "running"])
        self.assertEqual(
            self.controller.state["tasks"][second["task_id"]]["resource_requirements"]["access"],
            "read",
        )

    def test_worker_availability_explains_a_repository_block(self):
        controller, _ = self._same_repository_controller(
            worker_names=("one", "two"), limit=2, mode="serialized",
        )
        first = controller.submit_task(
            "block-1", "Write", "Edit", "one", True, None, "routine_development",
        )
        second = controller.submit_task(
            "block-2", "Write", "Edit", "two", True, None, "routine_development",
        )
        availability = controller.worker_availability()
        by_name = {item["worker"]: item for item in availability["workers"]}
        self.assertTrue(by_name["two"]["idle"])
        self.assertTrue(by_name["two"]["available"])
        self.assertFalse(by_name["two"]["runnable"])
        self.assertEqual(by_name["two"]["reason"], "repository_serialized")
        self.assertEqual(by_name["two"]["next_task_id"], second["task_id"])
        self.assertEqual(by_name["one"]["reason"], "worker_busy")
        self.assertEqual(availability["repository_parallelism"], "serialized")
        self.assertEqual(
            controller.state["tasks"][first["task_id"]]["status"], "running",
        )

    def test_repository_writer_conflict_blocks_second_profile_for_same_repository(self):
        self.controller.close()
        other = Path(self.temp.name) / "other-checkout"
        other.mkdir()
        ids = iter([
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ])
        profiles = {
            "one": RepositoryProfile("one", "owner/shared", self.root, "origin/main"),
            "two": RepositoryProfile("two", "OWNER/SHARED", other, "origin/main"),
        }
        self.controller = TaskController(
            Config(
                "owner/shared", self.root, self.state, "origin/main",
                {"one": Worker("one", ("codex",)), "two": Worker("two", ("codex",))},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                repository_profiles=profiles, default_profile="one",
                task_approval_mode="disabled", max_parallel_tasks=2,
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: next(ids),
        )
        first = self.controller.submit_task(
            "writer-one", "Write one", "Edit files", "one", True, "one", "routine_development",
        )
        second = self.controller.submit_task(
            "writer-two", "Write two", "Edit files", "two", True, "two", "routine_development",
        )

        self.assertEqual(first["status"], "running")
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(self.popen.call_count, 1)

    def test_repository_round_robin_prevents_hot_repository_starvation(self):
        self.controller.close()
        other = Path(self.temp.name) / "fair-repo"
        other.mkdir()
        ids = iter([
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
        ])
        profiles = {
            "hot": RepositoryProfile("hot", "owner/hot", self.root, "origin/main"),
            "other": RepositoryProfile("other", "owner/other", other, "origin/main"),
        }
        self.controller = TaskController(
            Config(
                "owner/hot", self.root, self.state, "origin/main",
                {name: Worker(name, ("codex",)) for name in ("hot-1", "hot-2", "other")},
                Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
                repository_profiles=profiles, default_profile="hot",
                task_approval_mode="disabled", max_parallel_tasks=1,
            ),
            popen=self.popen, workspace_preparer=lambda *_: self.root,
            id_factory=lambda: next(ids),
        )
        first = self.controller.submit_task(
            "hot-1", "Hot one", "Edit", "hot-1", True, "hot", "routine_development",
        )
        hot_waiting = self.controller.submit_task(
            "hot-2", "Hot two", "Edit", "hot-2", True, "hot", "routine_development",
        )
        other_waiting = self.controller.submit_task(
            "other", "Other", "Edit", "other", True, "other", "routine_development",
        )
        self.complete(first["task_id"])
        self.controller.tick()

        self.assertEqual(self.controller.task_status(other_waiting["task_id"])["status"], "running")
        self.assertEqual(self.controller.task_status(hot_waiting["task_id"])["status"], "accepted")

    def test_restart_adopts_matching_runner_lease_and_rejects_tampered_lease(self):
        submitted = self.controller.submit_task(
            "lease", "Lease", "Recover", "ina", True,
        )
        task = self.controller.state["tasks"][submitted["task_id"]]
        self.controller._start = Mock()
        self.controller.resolve_approval(
            submitted["task_id"], "approve", "approved", "local:test-operator",
        )
        lease = task["startup_lease"]
        lease_path = Path(lease["path"])
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(json.dumps({
            "task_id": task["task_id"], "lease_id": lease["lease_id"],
            "pid": os.getpid(), "started_at": "2026-10-19T00:00:00+00:00",
        }))
        self.controller.close()
        self.controller = TaskController(
            self.config, popen=self.popen, workspace_preparer=lambda *_: self.root,
        )
        self.assertEqual(self.controller.state["tasks"][task["task_id"]]["status"], "running")

        recovered = self.controller.state["tasks"][task["task_id"]]
        recovered.pop("runner_pid")
        recovered["status"] = "starting"
        recovered["startup_lease"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        lease_path.write_text(json.dumps({
            "task_id": task["task_id"], "lease_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "pid": os.getpid(), "started_at": "2026-10-19T00:00:00+00:00",
        }))
        self.controller.reconcile()
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["failure"]["type"], "startup_lease_expired")
        self.assertEqual(recovered["reservation"]["status"], "released")

    def test_cancellation_during_startup_lease_becomes_terminal(self):
        submitted = self.controller.submit_task(
            "cancel-starting", "Cancel startup", "Wait for cancellation", "ina", True,
        )
        self.controller._start = Mock()
        self.controller.resolve_approval(
            submitted["task_id"], "approve", "approved", "local:test-operator",
        )
        task = self.controller.state["tasks"][submitted["task_id"]]

        cancelled = self.controller.cancel_task(submitted["task_id"], "stop during startup")
        self.assertEqual(cancelled["status"], "cancel_requested")
        task["startup_lease"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        self.controller.reconcile()

        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(task["reservation"]["status"], "released")




class ConfigTests(unittest.TestCase):
    def test_profiles_define_independent_git_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            a_root = base / "repo-a"
            b_root = base / "repo-b"
            for path in (root, a_root, b_root):
                path.mkdir()
            codex = base / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o700)
            config_path = base / "omcp.json"
            config_path.write_text(json.dumps({
                "repository": "owner/orchestra",
                "project_root": str(root),
                "state_dir": str(base / "state"),
                "workers": {"ina": {"command": ["codex"]}},
                "principal": {"identity": "chatgpt:test", "permissions": ["tasks:submit"]},
                "local_worker_git_mode": "owner_synced",
                "local_worker_source_sha": "c" * 40,
                "repository_profiles": {
                    "orchestra": {
                        "repository": "owner/orchestra",
                        "project_root": str(a_root),
                        "base_ref": "origin/main",
                        "local_worker_git_mode": "owner_synced",
                        "local_worker_source_sha": "a" * 40,
                    },
                    "delnavazan": {
                        "repository": "owner/delnavazan",
                        "project_root": str(b_root),
                        "base_ref": "origin/main",
                        "local_worker_git_mode": "fetch",
                        "local_worker_source_sha": None,
                    },
                },
                "default_profile": "orchestra",
            }))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                config = Config.load(config_path)
        orchestra = config.profile("orchestra")
        delnavazan = config.profile("delnavazan")
        self.assertEqual(orchestra.local_worker_git_mode, "owner_synced")
        self.assertEqual(orchestra.local_worker_source_sha, "a" * 40)
        self.assertEqual(delnavazan.local_worker_git_mode, "fetch")
        self.assertIsNone(delnavazan.local_worker_source_sha)
        self.assertEqual(config.local_worker_git_mode, "owner_synced")
        self.assertEqual(config.local_worker_source_sha, "c" * 40)

    def test_legacy_profile_inherits_global_git_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            codex = base / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o700)
            config_path = base / "omcp.json"
            config_path.write_text(json.dumps({
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(base / "state"),
                "workers": {"ina": {"command": ["codex"]}},
                "principal": {"identity": "chatgpt:test", "permissions": ["tasks:submit"]},
                "local_worker_git_mode": "owner_synced",
                "local_worker_source_sha": "d" * 40,
            }))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                config = Config.load(config_path)
        profile = config.profile(None)
        self.assertEqual(profile.local_worker_git_mode, "owner_synced")
        self.assertEqual(profile.local_worker_source_sha, "d" * 40)

    def test_profile_owner_synced_requires_a_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            repo = base / "repo"
            root.mkdir(); repo.mkdir()
            codex = base / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o700)
            config_path = base / "omcp.json"
            config_path.write_text(json.dumps({
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(base / "state"),
                "workers": {"ina": {"command": ["codex"]}},
                "principal": {"identity": "chatgpt:test", "permissions": ["tasks:submit"]},
                "repository_profiles": {
                    "repo": {
                        "repository": "owner/repo", "project_root": str(repo),
                        "base_ref": "origin/main", "local_worker_git_mode": "owner_synced",
                    },
                },
                "default_profile": "repo",
            }))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                with self.assertRaisesRegex(ValueError, "owner_synced requires"):
                    Config.load(config_path)

    def test_deepseek_worker_cannot_receive_standing_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            credential = base / "deepseek.key"
            credential.write_text("not-printed")
            credential.chmod(0o600)
            config_path = base / "omcp.json"
            config_path.write_text(json.dumps({
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(base / "state"),
                "workers": {"deepseek": {
                    "adapter": "deepseek", "provider": "deepseek",
                    "model": "deepseek-flash",
                    "credential_file": str(credential),
                }},
                "principal": {"identity": "chatgpt:test", "permissions": ["tasks:submit"]},
                "standing_authorizations": [{
                    "id": "deepseek-routine",
                    "consequence_class": "routine_development",
                    "repository_profiles": ["default"],
                    "workers": ["deepseek"],
                    "writable_paths": ["platform"],
                }],
            }))
            os.chmod(config_path, 0o600)
            with self.assertRaisesRegex(ValueError, "only name Codex workers"):
                Config.load(config_path)

    def test_standing_authorization_rejects_unsafe_writable_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            codex = base / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o700)
            config_path = base / "omcp.json"
            config_path.write_text(json.dumps({
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(base / "state"),
                "workers": {"ina": {"command": ["codex"]}},
                "principal": {"identity": "chatgpt:test", "permissions": ["tasks:submit"]},
                "standing_authorizations": [{
                    "id": "routine", "consequence_class": "routine_development",
                    "repository_profiles": ["default"], "workers": ["ina"],
                    "writable_paths": ["../outside"],
                }],
            }))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                with self.assertRaisesRegex(ValueError, "safe relative paths"):
                    Config.load(config_path)

    def test_config_loads_independent_allowlisted_opencode_and_deepseek_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            credential = Path(directory) / "deepseek.key"
            credential.write_text("not-printed")
            credential.chmod(0o600)
            opencode_credential = Path(directory) / "opencode.key"
            opencode_credential.write_text("not-printed")
            opencode_credential.chmod(0o600)
            config_path = Path(directory) / "omcp.json"
            config_path.write_text(json.dumps({
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(Path(directory) / "state"),
                "opencode_model_allowlist": {"openai": ["openai/gpt-4.1"]},
                "workers": {
                    "opencode": {
                        "adapter": "opencode",
                        "command": ["/allowlisted/opencode-cli"],
                        "provider": "openai", "model": "openai/gpt-4.1",
                        "credential_file": str(opencode_credential),
                    },
                    "deepseek": {
                        "adapter": "deepseek",
                        "provider": "deepseek", "model": "deepseek-flash",
                        "credential_file": str(credential),
                    },
                },
                "principal": {"identity": "chatgpt:test", "permissions": ["tasks:submit"]},
                "max_worker_seconds": 600,
            }))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.canonical_opencode_executable", return_value="/allowlisted/opencode-cli"):
                config = Config.load(config_path)
            self.assertEqual(config.max_worker_seconds, 600)
            self.assertEqual(config.workers["opencode"].adapter, "opencode")
            self.assertEqual(config.workers["opencode"].provider, "openai")
            self.assertEqual(config.workers["deepseek"].adapter, "deepseek")
            self.assertEqual(config.workers["deepseek"].provider, "deepseek")
            self.assertEqual(config.workers["deepseek"].credential_file, str(credential.resolve()))

    def test_config_rejects_unapproved_opencode_provider_command_and_credential_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            credential = Path(directory) / "deepseek.key"
            credential.write_text("not-printed")
            credential.chmod(0o644)
            config_path = Path(directory) / "omcp.json"
            raw = {
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(Path(directory) / "state"),
                "workers": {"deepseek": {
                    "adapter": "opencode", "command": ["/bin/sh", "-c", "anything"],
                    "provider": "other", "model": "other/model", "credential_file": str(credential),
                }},
                "principal": {"identity": "chatgpt:test", "permissions": ["tasks:submit"]},
            }
            config_path.write_text(json.dumps(raw))
            os.chmod(config_path, 0o600)
            with self.assertRaisesRegex(ValueError, "may not contain CLI arguments"):
                Config.load(config_path)
            raw["workers"]["deepseek"]["command"] = ["/allowlisted/opencode-cli"]
            config_path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "opencode_model_allowlist"):
                Config.load(config_path)
            raw["workers"]["deepseek"].update({
                "provider": "deepseek", "model": "deepseek/deepseek-chat",
            })
            raw["opencode_model_allowlist"] = {"deepseek": ["deepseek/deepseek-chat"]}
            config_path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "owner-only regular file"):
                Config.load(config_path)

    def test_config_loads_only_built_in_pinned_host_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            codex = Path(directory) / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o700)
            config_path = Path(directory) / "omcp.json"
            value = {
                "repository": "owner/repo", "project_root": str(root),
                "state_dir": str(Path(directory) / "state"),
                "workers": {"ina": {"command": [str(codex)]}},
                "principal": {
                    "identity": "chatgpt:test",
                    "permissions": ["host_operations:submit"],
                },
                "host_operations": {
                    "delnavazan-mariadb": {
                        "type": "delnavazan_mariadb_acceptance_v1",
                        "repository_profile": "default",
                        "harness_commit": "2" * 40,
                        "timeout_seconds": 1800,
                    },
                },
            }
            config_path.write_text(json.dumps(value))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                loaded = Config.load(config_path)
            operation = loaded.host_operations["delnavazan-mariadb"]
            self.assertEqual(operation.harness_commit, "2" * 40)
            self.assertEqual(operation.timeout_seconds, 1800)
            value["host_operations"]["delnavazan-mariadb"]["command"] = ["/bin/sh"]
            config_path.write_text(json.dumps(value))
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                with self.assertRaisesRegex(ValueError, "unsupported fields"):
                    Config.load(config_path)

    def test_config_loads_and_validates_task_approval_mode(self):
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
                "task_approval_mode": "disabled",
            }
            config_path.write_text(json.dumps(raw))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                self.assertEqual(Config.load(config_path).task_approval_mode, "disabled")
                raw["task_approval_mode"] = "sometimes"
                config_path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, "must be required or disabled"):
                    Config.load(config_path)

    def test_config_loads_and_validates_local_worker_launcher(self):
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
                "local_worker_launcher": "systemd-user",
            }
            config_path.write_text(json.dumps(raw))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                self.assertEqual(Config.load(config_path).local_worker_launcher, "systemd-user")
                raw["local_worker_launcher"] = "sometimes"
                config_path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, "direct or systemd-user"):
                    Config.load(config_path)

    def test_config_loads_retired_instance_identity(self):
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
                "principal": {"identity": "chatgpt:test", "permissions": ["tasks:read"]},
                "instance_role": "retired",
                "authoritative_instance_id": "11111111-1111-1111-1111-111111111111",
            }
            config_path.write_text(json.dumps(raw))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                config = Config.load(config_path)
                self.assertEqual(config.instance_role, "retired")
                self.assertEqual(
                    config.authoritative_instance_id,
                    "11111111-1111-1111-1111-111111111111",
                )
                raw["instance_role"] = "authoritative"
                config_path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, "only valid for a retired"):
                    Config.load(config_path)
                raw["instance_role"] = "primary"
                config_path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, "authoritative or retired"):
                    Config.load(config_path)

    def test_config_repository_parallelism_defaults_off_and_validates_mode(self):
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
            }
            config_path.write_text(json.dumps(raw))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                self.assertEqual(
                    Config.load(config_path).repository_parallelism, "serialized",
                )
                raw["repository_parallelism"] = "isolated_worktrees"
                config_path.write_text(json.dumps(raw))
                self.assertEqual(
                    Config.load(config_path).repository_parallelism,
                    "isolated_worktrees",
                )
                raw["repository_parallelism"] = "unbounded"
                config_path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, "repository_parallelism"):
                    Config.load(config_path)

    def test_config_supports_future_parallel_capacity_without_hardcoded_two(self):
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
                "max_parallel_tasks": 8, "startup_lease_seconds": 45,
            }
            config_path.write_text(json.dumps(raw))
            os.chmod(config_path, 0o600)
            with patch("omcp.controller.shutil.which", return_value=str(codex)):
                config = Config.load(config_path)
                self.assertEqual(config.max_parallel_tasks, 8)
                self.assertEqual(config.startup_lease_seconds, 45)
                raw["max_parallel_tasks"] = 65
                config_path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, "between 1 and 64"):
                    Config.load(config_path)

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
    def test_opencode_policy_uses_v2_schema_and_only_local_gateway(self):
        gateway = "http://127.0.0.1:49152/abcdefghijklmnopqrstuvwxyzABCDEFG_123456/v1"
        policy = opencode_policy("deepseek", "deepseek/deepseek-chat", gateway)
        self.assertEqual(policy["policies"], [
            {"action": "provider.use", "resource": "*", "effect": "deny"},
            {"action": "provider.use", "resource": "deepseek", "effect": "allow"},
        ])
        provider = policy["provider"]["deepseek"]
        self.assertEqual(provider["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(provider["models"], {"deepseek-chat": {}})
        self.assertEqual(provider["options"]["timeout"], 1_800_000)
        self.assertEqual(provider["options"]["baseURL"], gateway)
        self.assertEqual(provider["options"]["apiKey"], "omcp-local-gateway")
        self.assertNotIn("api.deepseek.com", json.dumps(policy))
        self.assertFalse(policy["snapshots"])

    def test_opencode_commands_have_only_exact_loopback_egress_and_no_credential_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            prompt = root / "prompt"
            prompt.write_text("task")
            runtime = root / "runtime"
            gateway = "http://127.0.0.1:49152/abcdefghijklmnopqrstuvwxyzABCDEFG_123456/v1"

            with patch("omcp.platform.host_platform", return_value="darwin"), \
                    patch("omcp.platform.sandbox_executable",
                          return_value="/usr/bin/sandbox-exec"):
                server, client = opencode_commands(
                    "/allowlisted/opencode-cli", workspace, prompt, runtime,
                    "deepseek", "deepseek/deepseek-chat", "api.deepseek.com", gateway, 49153,
                )

            server_profile, client_profile = server[2], client[2]
            profiles = server_profile + client_profile
            self.assertIn(
                '(allow process-exec (literal "/allowlisted/opencode-cli"))',
                server_profile,
            )
            self.assertIn(
                '(allow process-exec (literal "/allowlisted/opencode-cli"))',
                client_profile,
            )
            self.assertNotIn("api.deepseek.com", profiles)
            self.assertNotIn("*:443", profiles)
            self.assertNotIn("localhost:*", profiles)
            self.assertNotIn('tcp "*:*"', profiles)
            self.assertIn('(allow network-bind (local tcp "localhost:49153"))', server_profile)
            self.assertIn('(allow network-outbound (remote tcp "localhost:49152"))', server_profile)
            self.assertIn('(allow network-outbound (remote tcp "localhost:49153"))', client_profile)
            self.assertNotIn("localhost:49152", client_profile)
            self.assertIn("serve", server)
            self.assertIn("--server", client)
            self.assertNotIn("--standalone", client)
            config = (runtime / "config" / "opencode" / "opencode.json").read_text()
            self.assertNotIn("credential", config)
            self.assertIn(gateway, config)

    def test_opencode_policy_rejects_cross_provider_model(self):
        gateway = "http://127.0.0.1:49152/abcdefghijklmnopqrstuvwxyzABCDEFG_123456/v1"
        with self.assertRaisesRegex(ValueError, "gateway URL"):
            opencode_policy("deepseek", "openai/gpt-4.1", gateway)

    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
        "requires macOS Seatbelt",
    )
    def test_opencode_exact_port_seatbelt_profiles_compile_on_macos(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            prompt = root / "prompt"
            prompt.write_text("offline synthetic probe")
            runtime = root / "runtime"
            server, client = opencode_commands(
                "/usr/bin/true", workspace, prompt, runtime,
                "deepseek", "deepseek/deepseek-chat", "api.deepseek.com",
                "http://127.0.0.1:49152/abcdefghijklmnopqrstuvwxyzABCDEFG_123456/v1",
                49153,
            )
            baseline = subprocess.run(
                ["/usr/bin/sandbox-exec", "-p", "(version 1)\n(allow default)",
                 "/usr/bin/true"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                timeout=10,
            )
            if (baseline.returncode != 0
                    and b"Operation not permitted" in baseline.stderr):
                self.skipTest("enclosing sandbox blocks sandbox execution")
            self.assertEqual(
                baseline.returncode, 0, baseline.stderr.decode("utf-8", "replace"),
            )
            for command in (server, client):
                probe = subprocess.run(
                    ["/usr/bin/sandbox-exec", "-p", command[2], "/usr/bin/true"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    timeout=10,
                )
                self.assertEqual(probe.returncode, 0, probe.stderr.decode("utf-8", "replace"))

    def test_pinned_gateway_offline_synthetic_key_probe_uses_no_real_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "synthetic.key"
            credential.write_text("offline-synthetic-key")
            credential.chmod(0o600)
            gateway = PinnedProviderGateway(
                "deepseek", "deepseek/deepseek-chat", "api.deepseek.com", credential, 60,
            )
            try:
                gateway_url = gateway.start()
            except PermissionError as exc:
                if exc.errno == 1:
                    self.skipTest("enclosing sandbox prevents loopback bind")
                raise
            url = urlparse(gateway_url)
            upstream_response = Mock(status=401)
            upstream_response.getheader.side_effect = lambda name, default=None: {
                "Content-Type": "application/json",
            }.get(name, default)
            upstream_response.read.side_effect = [b'{"error":{"message":"synthetic"}}', b""]
            upstream = Mock()
            upstream.getresponse.return_value = upstream_response
            payload = json.dumps({
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "offline"}],
                "stream": False,
            })
            try:
                with patch("omcp.runner.http.client.HTTPSConnection",
                           return_value=upstream) as connect:
                    local = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
                    local.request(
                        "POST", url.path + "/chat/completions", payload,
                        {"Authorization": "Bearer omcp-local-gateway",
                         "Content-Type": "application/json"},
                    )
                    response = local.getresponse()
                    body = response.read()
                    local.close()
                self.assertEqual(response.status, 401)
                self.assertEqual(connect.call_args.args[:2], ("api.deepseek.com", 443))
                self.assertEqual(
                    upstream.request.call_args.args[3]["Authorization"],
                    "Bearer offline-synthetic-key",
                )
                self.assertNotIn(b"offline-synthetic-key", body)
            finally:
                gateway.close()

    def test_pinned_gateway_forwards_only_exact_deepseek_request_with_verified_tls(self):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "deepseek.key"
            credential.write_text("synthetic-secret-never-retain")
            credential.chmod(0o600)
            gateway = PinnedProviderGateway(
                "deepseek", "deepseek/deepseek-chat", "api.deepseek.com", credential, 600,
            )
            gateway._server = Mock(server_port=49152)
            payload = json.dumps({
                "model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            }).encode()
            handler = Mock()
            handler.path = f"/{gateway._token}/v1/chat/completions"
            handler.headers = {
                "Host": "127.0.0.1:49152", "Authorization": "Bearer omcp-local-gateway",
                "Content-Type": "application/json", "Content-Length": str(len(payload)),
            }
            handler.rfile = io.BytesIO(payload)
            handler.wfile = io.BytesIO()
            handler.close_connection = False
            response = Mock(status=200)
            response.getheader.side_effect = lambda name, default=None: {
                "Content-Type": "text/event-stream",
            }.get(name, default)
            response.read.side_effect = [b"data: safe\n\n", b""]
            connection = Mock()
            connection.getresponse.return_value = response

            with patch("omcp.runner.http.client.HTTPSConnection", return_value=connection) as connect:
                gateway._serve_request(handler)

            self.assertEqual(connect.call_args.args[:2], ("api.deepseek.com", 443))
            context = connect.call_args.kwargs["context"]
            ssl_module = __import__("ssl")
            self.assertIsInstance(context, ssl_module.SSLContext)
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl_module.CERT_REQUIRED)
            request = connection.request.call_args
            self.assertEqual(request.args[:2], ("POST", "/v1/chat/completions"))
            self.assertEqual(request.args[3]["Authorization"], "Bearer synthetic-secret-never-retain")
            self.assertNotIn("synthetic-secret-never-retain", handler.wfile.getvalue().decode())
            gateway.close()

    def test_pinned_gateway_rejects_wrong_route_without_upstream_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "deepseek.key"
            credential.write_text("synthetic-secret")
            credential.chmod(0o600)
            gateway = PinnedProviderGateway(
                "deepseek", "deepseek/deepseek-chat", "api.deepseek.com", credential, 600,
            )
            gateway._server = Mock(server_port=49152)
            handler = Mock()
            handler.path = "/v1/chat/completions"
            handler.headers = {}
            handler.rfile = io.BytesIO()
            handler.wfile = io.BytesIO()
            handler.close_connection = False

            with patch("omcp.runner.http.client.HTTPSConnection") as connect:
                gateway._serve_request(handler)

            connect.assert_not_called()
            self.assertTrue(handler.close_connection)
            gateway.close()

    def test_pinned_gateway_rejects_model_encoding_size_and_request_count_deviations(self):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "deepseek.key"
            credential.write_text("synthetic-secret")
            credential.chmod(0o600)
            gateway = PinnedProviderGateway(
                "deepseek", "deepseek/deepseek-chat", "api.deepseek.com", credential, 600,
            )
            gateway._server = Mock(server_port=49152)

            def rejected(payload: bytes, **headers: str) -> None:
                gateway._request_count = 0
                handler = Mock()
                handler.path = f"/{gateway._token}/v1/chat/completions"
                handler.headers = {
                    "Host": "127.0.0.1:49152", "Authorization": "Bearer omcp-local-gateway",
                    "Content-Type": "application/json", "Content-Length": str(len(payload)),
                    **headers,
                }
                handler.rfile = io.BytesIO(payload)
                handler.wfile = io.BytesIO()
                handler.close_connection = False
                gateway._serve_request(handler)
                self.assertTrue(handler.close_connection)

            wrong_model = json.dumps({"model": "other", "messages": []}).encode()
            valid = json.dumps({"model": "deepseek-chat", "messages": []}).encode()
            with patch("omcp.runner.http.client.HTTPSConnection") as connect:
                rejected(wrong_model)
                rejected(valid, **{"Transfer-Encoding": "chunked"})
                rejected(valid, **{"Content-Encoding": "gzip"})
                rejected(valid, **{"Content-Length": str(gateway.MAX_REQUEST_BYTES + 1)})
                gateway._request_count = gateway.MAX_REQUESTS
                handler = Mock()
                handler.path = f"/{gateway._token}/v1/chat/completions"
                handler.headers = {
                    "Host": "127.0.0.1:49152", "Authorization": "Bearer omcp-local-gateway",
                    "Content-Type": "application/json", "Content-Length": str(len(valid)),
                }
                handler.rfile, handler.wfile = io.BytesIO(valid), io.BytesIO()
                gateway._serve_request(handler)
                connect.assert_not_called()
            gateway.close()

    def test_opencode_diagnostics_reduce_provider_output_without_retaining_content(self):
        collector = OpenCodeDiagnosticCollector()
        secret = "credential-never-retained"
        prompt = "prompt-never-retained"
        model_output = "model-output-never-retained"
        collector.feed((json.dumps({
            "type": "error",
            "error": {"type": "provider.auth", "status": 429,
                      "message": prompt, "response": {"body": secret + model_output}},
        }) + "\n").encode())
        collector.finish()
        diagnostic = collector.result(1)
        self.assertEqual(diagnostic, {
            "category": "rate_limit", "status": 429, "exit_cause": "process_exit",
        })
        serialized = json.dumps(diagnostic)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(prompt, serialized)
        self.assertNotIn(model_output, serialized)

    def test_opencode_process_discards_raw_stream_and_reports_http_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir()
            raw = json.dumps({
                "type": "error", "error": {"type": "provider.auth", "status": 403,
                "message": "secret diagnostic"},
            })
            code, diagnostic = run_opencode_process(
                [sys.executable, "-c", "import sys; print(sys.argv[1]); raise SystemExit(1)", raw],
                root, {"PATH": os.environ.get("PATH", "")}, runtime, 60,
            )
            self.assertEqual(code, 1)
            self.assertEqual(diagnostic, {
                "category": "authentication", "status": 403, "exit_cause": "process_exit",
            })
            self.assertEqual(list(runtime.iterdir()), [])
            self.assertNotIn("secret diagnostic", json.dumps(diagnostic))

    def test_deepseek_runner_uses_direct_https_and_never_retains_secret_or_raw_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt"
            prompt.write_text("task")
            credential = root / "deepseek.key"
            credential.write_text("never-log-this")
            credential.chmod(0o600)
            spec = root / "spec.json"
            spec.write_text(json.dumps({
                "adapter": "deepseek", "command": [],
                "provider": "deepseek", "model": "deepseek-flash",
                "endpoint_host": "api.deepseek.com", "max_response_bytes": 1024,
                "credential_file": str(credential), "max_worker_seconds": 600,
                "workspace": str(root), "prompt_path": str(prompt),
                "log_path": str(root / "log"), "result_path": str(root / "result"),
                "completion_path": str(root / "completion"),
            }))
            parent_environment = {
                "PATH": "/trusted/bin", "OPENAI_API_KEY": "tunnel-secret",
                "DEEPSEEK_API_KEY": "other-secret", "OMCP_AUTHENTICATED_CALLER": "principal",
            }
            response = Mock(status=200)
            response.read.return_value = b'{"choices":[{"message":{"content":"safe response"}}]}'
            connection = Mock()
            connection.getresponse.return_value = response
            with patch.dict(os.environ, parent_environment, clear=True), \
                    patch("omcp.runner.http.client.HTTPSConnection", return_value=connection) as connect, \
                    patch("omcp.runner.signal.setitimer") as setitimer:
                self.assertEqual(run_worker(spec), 0)
            self.assertEqual(connect.call_args.args[:2], ("api.deepseek.com", 443))
            sent_payload = json.loads(connection.request.call_args.args[2])
            self.assertEqual(sent_payload["model"], "deepseek-flash")
            self.assertEqual(setitimer.call_args_list[0].args, (signal.ITIMER_REAL, 600))
            self.assertEqual(setitimer.call_args_list[-1].args, (signal.ITIMER_REAL, 0))
            self.assertFalse((root / "log").exists())
            self.assertEqual((root / "result").read_text(), "safe response")
            self.assertNotIn("never-log-this", (root / "completion").read_text())

    def test_opencode_runner_rejects_incomplete_spec_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt"
            prompt.write_text("task")
            spec = root / "spec.json"
            spec.write_text(json.dumps({
                "adapter": "opencode", "command": ["/allowlisted/opencode-cli"],
                "provider": "other", "model": "other/model", "max_worker_seconds": 600,
                "workspace": str(root), "prompt_path": str(prompt),
                "log_path": str(root / "log"), "result_path": str(root / "result"),
                "completion_path": str(root / "completion"),
            }))
            with patch("omcp.runner.canonical_opencode_executable", return_value="/allowlisted/opencode-cli"), \
                    patch("omcp.runner.subprocess.run") as execute:
                with self.assertRaisesRegex(ValueError, "specification is incomplete"):
                    run_worker(spec)
                execute.assert_not_called()

    def test_opencode_runner_uses_gateway_and_records_only_redacted_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt"
            prompt.write_text("sensitive prompt")
            credential = root / "opencode.key"
            credential.write_text("sensitive credential")
            credential.chmod(0o600)
            completion = root / "completion"
            spec = root / "spec.json"
            spec.write_text(json.dumps({
                "adapter": "opencode", "command": ["/allowlisted/opencode-cli"],
                "provider": "deepseek", "model": "deepseek/deepseek-chat",
                "endpoint_host": "api.deepseek.com", "credential_file": str(credential),
                "max_worker_seconds": 600, "workspace": str(root), "prompt_path": str(prompt),
                "log_path": str(root / "log"), "result_path": str(root / "result"),
                "completion_path": str(completion),
            }))
            safe = {"category": "authentication", "status": 401, "exit_cause": "process_exit"}
            gateway = Mock()
            gateway.start.return_value = (
                "http://127.0.0.1:49152/abcdefghijklmnopqrstuvwxyzABCDEFG_123456/v1"
            )
            with patch("omcp.runner.canonical_opencode_executable",
                       return_value="/allowlisted/opencode-cli"), \
                    patch("omcp.runner.PinnedProviderGateway", return_value=gateway) as gateway_type, \
                    patch("omcp.runner.reserve_loopback_port", return_value=49153), \
                    patch("omcp.runner.run_opencode_with_server", return_value=(1, safe)) as execute, \
                    patch("omcp.platform.host_platform", return_value="darwin"), \
                    patch("omcp.platform.sandbox_executable",
                          return_value="/usr/bin/sandbox-exec"):
                self.assertEqual(run_worker(spec), 1)

            gateway_type.assert_called_once_with(
                "deepseek", "deepseek/deepseek-chat", "api.deepseek.com", credential, 600,
            )
            gateway.close.assert_called_once_with()
            server_command, client_command = execute.call_args.args[:2]
            self.assertNotIn("api.deepseek.com", server_command[2] + client_command[2])
            self.assertNotIn("sensitive credential", json.dumps([server_command, client_command]))
            self.assertNotIn("sensitive credential", json.dumps(execute.call_args.args[3]))
            self.assertIn("OPENCODE_PASSWORD", execute.call_args.args[3])
            record = json.loads(completion.read_text())
            self.assertEqual(record["runner_error"], "OpenCodeExecutionFailed")
            self.assertEqual(record["worker_diagnostic"], safe)
            retained = completion.read_text() + (root / "result").read_text()
            self.assertNotIn("sensitive prompt", retained)
            self.assertNotIn("sensitive credential", retained)
            self.assertFalse((root / "log").exists())
            self.assertFalse((root / "opencode-runtime").exists())

    def test_deepseek_failure_is_sanitized_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credential = root / "deepseek.key"
            credential.write_text("not-printed")
            credential.chmod(0o600)
            prompt = root / "prompt"
            prompt.write_text("task")
            spec = root / "spec.json"
            spec.write_text(json.dumps({
                "adapter": "deepseek", "command": [], "provider": "deepseek",
                "model": "deepseek-flash", "endpoint_host": "api.deepseek.com",
                "credential_file": str(credential), "max_worker_seconds": 60,
                "max_response_bytes": 1024, "workspace": str(root), "prompt_path": str(prompt),
                "log_path": str(root / "log"), "result_path": str(root / "result"),
                "completion_path": str(root / "completion"),
            }))
            with patch("omcp.runner.http.client.HTTPSConnection", side_effect=OSError("secret diagnostic")):
                self.assertEqual(run_worker(spec), 1)
            self.assertEqual((root / "result").read_text(),
                             "DeepSeek request failed; no provider response was retained.\n")
            self.assertNotIn("secret diagnostic", (root / "completion").read_text())
    def test_sandbox_probe_fails_closed_on_nonzero_bubblewrap(self):
        with patch("omcp.runner.host_platform", return_value="linux"), \
                patch("omcp.runner.shutil.which",
                      side_effect=lambda name: {"bwrap": "/usr/bin/bwrap",
                                                "true": "/bin/true"}[name]), \
                patch("omcp.runner.subprocess.run", return_value=Mock(returncode=1)):
            with self.assertRaises(WorkerSandboxStartupError):
                probe_codex_sandbox()
        with patch("omcp.runner.host_platform", return_value="linux"), \
                patch("omcp.runner.shutil.which",
                      side_effect=lambda name: {"bwrap": "/usr/bin/bwrap",
                                                "true": "/bin/true"}[name]), \
                patch("omcp.runner.subprocess.run", return_value=Mock(returncode=0)):
            probe_codex_sandbox()

    def test_sandbox_log_detection_requires_a_bwrap_error_line(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "worker.log"
            log.write_text("agent quoted: bwrap: Can't open source /: Function not implemented\n")
            self.assertFalse(worker_log_has_sandbox_startup_failure(log))
            log.write_text("  bwrap: Can't open source /: Function not implemented\n")
            self.assertTrue(worker_log_has_sandbox_startup_failure(log))

    def test_probe_failure_is_failed_and_skips_codex_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            marker = root / "codex-ran"
            codex = bin_dir / "codex"
            codex.write_text(f"#!/bin/sh\n: > {marker}\nexit 0\n")
            codex.chmod(0o700)
            prompt = root / "prompt"
            prompt.write_text("task")
            spec = root / "spec.json"
            completion = root / "completion.json"
            spec.write_text(json.dumps({
                "command": ["codex"], "workspace": str(root),
                "prompt_path": str(prompt), "log_path": str(root / "log"),
                "result_path": str(root / "result"),
                "completion_path": str(completion), "codex_home": None,
                "sandbox_startup_probe": True,
            }))
            with patch.dict(os.environ, {"PATH": str(bin_dir)}, clear=True), \
                    patch("omcp.runner.probe_codex_sandbox",
                          side_effect=WorkerSandboxStartupError("failed")):
                self.assertEqual(run_worker(spec), 125)
            self.assertFalse(marker.exists())
            record = json.loads(completion.read_text())
            self.assertEqual(record["exit_code"], 125)
            self.assertEqual(record["runner_error"], "SandboxStartupError")
            self.assertIn("sandbox failed to start", (root / "result").read_text().lower())

    def test_logged_bubblewrap_failure_overrides_a_zero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            codex = bin_dir / "codex"
            codex.write_text(
                "#!/bin/sh\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = \"--output-last-message\" ]; then shift; result=\"$1\"; fi\n"
                "  shift\n"
                "done\n"
                "echo \"bwrap: Can't open source /: Function not implemented\" >&2\n"
                "echo 'Both checks failed because the sandbox did not start.' > \"$result\"\n"
                "exit 0\n"
            )
            codex.chmod(0o700)
            prompt = root / "prompt"
            prompt.write_text("task")
            spec = root / "spec.json"
            completion = root / "completion.json"
            spec.write_text(json.dumps({
                "command": ["codex"], "workspace": str(root),
                "prompt_path": str(prompt), "log_path": str(root / "log"),
                "result_path": str(root / "result"),
                "completion_path": str(completion), "codex_home": None,
            }))
            with patch.dict(os.environ, {"PATH": str(bin_dir)}, clear=True):
                self.assertEqual(run_worker(spec), 125)
            record = json.loads(completion.read_text())
            self.assertEqual(record["exit_code"], 125)
            self.assertEqual(record["runner_error"], "SandboxStartupError")
            self.assertIn("Both checks failed", (root / "result").read_text())
            self.assertIn("bwrap: Can't open source", (root / "log").read_text())

    def test_zero_exit_without_required_result_is_failed(self):
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
            completion = root / "completion.json"
            spec.write_text(json.dumps({
                "command": ["codex"], "workspace": str(root),
                "prompt_path": str(prompt), "log_path": str(root / "log"),
                "result_path": str(root / "result"),
                "completion_path": str(completion), "codex_home": None,
            }))
            with patch.dict(os.environ, {"PATH": str(bin_dir)}, clear=True):
                self.assertEqual(run_worker(spec), 125)
            record = json.loads(completion.read_text())
            self.assertEqual(record["runner_error"], "MissingWorkerResult")
            self.assertIn("required result", (root / "result").read_text())

    def test_zero_exit_with_required_result_and_no_optional_artifact_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            codex = bin_dir / "codex"
            codex.write_text(
                "#!/bin/sh\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = \"--output-last-message\" ]; then shift; result=\"$1\"; fi\n"
                "  shift\n"
                "done\n"
                "echo 'read-only canary completed' > \"$result\"\n"
                "exit 0\n"
            )
            codex.chmod(0o700)
            prompt = root / "prompt"
            prompt.write_text("task")
            spec = root / "spec.json"
            completion = root / "completion.json"
            spec.write_text(json.dumps({
                "command": ["codex"], "workspace": str(root),
                "prompt_path": str(prompt), "log_path": str(root / "log"),
                "result_path": str(root / "result"),
                "completion_path": str(completion), "codex_home": None,
            }))
            with patch.dict(os.environ, {"PATH": str(bin_dir)}, clear=True):
                self.assertEqual(run_worker(spec), 0)
            record = json.loads(completion.read_text())
            self.assertEqual(record["exit_code"], 0)
            self.assertEqual((root / "result").read_text(), "read-only canary completed\n")

    def _contract_spec(self, root: Path) -> Path:
        prompt = root / "prompt"
        prompt.write_text("task")
        spec = root / "spec.json"
        spec.write_text(json.dumps({
            "command": ["codex"], "workspace": str(root),
            "prompt_path": str(prompt), "log_path": str(root / "log"),
            "result_path": str(root / "result"),
            "completion_path": str(root / "completion"),
            "codex_home": None,
            "require_completion_contract": True,
        }))
        return spec

    def test_runner_accepts_a_structured_success_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self._contract_spec(root)
            with patch.dict(os.environ, codex_contract_fixture(root), clear=True):
                self.assertEqual(run_worker(spec), 0)
            record = json.loads((root / "completion").read_text())
            self.assertEqual(record["completion_contract"], "success")
            self.assertNotIn("completion_contract_error", record)

    def test_runner_contract_failure_overrides_a_zero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self._contract_spec(root)
            fixture = codex_contract_fixture(
                root, result="required filesystem operation was denied",
                contract='{"status":"failure","reason":"required write denied"}',
            )
            with patch.dict(os.environ, fixture, clear=True):
                self.assertEqual(run_worker(spec), 125)
            record = json.loads((root / "completion").read_text())
            self.assertEqual(record["runner_error"], "TaskContractFailed")
            self.assertEqual(record["completion_contract"], "failure")
            self.assertIn("required filesystem operation", (root / "result").read_text())

    def test_runner_missing_contract_fails_even_when_result_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self._contract_spec(root)
            fixture = codex_contract_fixture(root, contract=None)
            with patch.dict(os.environ, fixture, clear=True):
                self.assertEqual(run_worker(spec), 125)
            record = json.loads((root / "completion").read_text())
            self.assertEqual(record["runner_error"], "TaskContractInvalid")
            self.assertEqual(record["completion_contract"], "invalid")

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
                "TMPDIR": str(root / ".orchestra-tmp"), "LANG": "en_AU.UTF-8",
                "LC_ALL": "en_AU.UTF-8", "CODEX_HOME": "/parent/codex-home",
                "CONTROL_PLANE_API_KEY": "tunnel-secret",
                "OMCP_AUTHENTICATED_CALLER": "chatgpt:principal",
                "OMCP_TUNNEL_AUTHENTICATED": "1", "OPENAI_API_KEY": "api-secret",
                "HTTPS_PROXY": "https://proxy.example", "GIT_TERMINAL_PROMPT": "1",
            }
            with patch.dict(os.environ, parent_environment, clear=True):
                with patch("omcp.runner.subprocess.run",
                           side_effect=codex_success_side_effect(root / "result")) as execute:
                    self.assertEqual(run_worker(spec), 0)

            environment = execute.call_args.kwargs["env"]
            self.assertEqual(environment, {
                "PATH": str(bin_dir), "HOME": "/Users/test",
                "TMPDIR": str(root / ".orchestra-tmp"), "LANG": "en_AU.UTF-8",
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
            with patch.dict(os.environ, codex_fixture(root), clear=True), \
                    patch("omcp.runner.subprocess.run",
                          side_effect=codex_success_side_effect(root / "result")) as execute:
                self.assertEqual(run_worker(spec), 0)
            self.assertEqual(json.loads(completion.read_text())["exit_code"], 0)
            command = execute.call_args.args[0]
            self.assertEqual(command.count("--sandbox"), 1)
            self.assertIn("--ask-for-approval", command)
            self.assertEqual(command[command.index("--ask-for-approval") + 1], "never")
            self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
            self.assertEqual(command[-1], "-")

    def test_runner_publishes_owner_generated_startup_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt"
            prompt.write_text("task")
            lease = root / "lease.json"
            spec = root / "runner.json"
            task_id = "11111111-1111-1111-1111-111111111111"
            lease_id = "22222222-2222-2222-2222-222222222222"
            spec.write_text(json.dumps({
                "command": ["codex"], "workspace": str(root),
                "prompt_path": str(prompt), "log_path": str(root / "log"),
                "result_path": str(root / "result"),
                "completion_path": str(root / "completion"),
                "task_id": task_id, "lease_id": lease_id, "lease_path": str(lease),
            }))
            with patch("omcp.runner.canonical_codex_executable", return_value="/codex"), \
                    patch("omcp.runner.subprocess.run",
                          side_effect=codex_success_side_effect(root / "result")):
                self.assertEqual(run_worker(spec), 0)
            record = json.loads(lease.read_text())
            self.assertEqual(record["task_id"], task_id)
            self.assertEqual(record["lease_id"], lease_id)
            self.assertEqual(record["pid"], os.getpid())

    def test_runner_rejects_startup_lease_outside_private_task_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt"
            prompt.write_text("task")
            spec = root / "runner.json"
            spec.write_text(json.dumps({
                "command": ["codex"], "workspace": str(root),
                "prompt_path": str(prompt), "log_path": str(root / "log"),
                "result_path": str(root / "result"),
                "completion_path": str(root / "completion"),
                "task_id": "11111111-1111-1111-1111-111111111111",
                "lease_id": "22222222-2222-2222-2222-222222222222",
                "lease_path": str(root.parent / "lease.json"),
            }))
            with patch("omcp.runner.subprocess.run") as execute:
                with self.assertRaisesRegex(ValueError, "private task directory"):
                    run_worker(spec)
                execute.assert_not_called()

    def test_runner_writes_completion_record_when_terminated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completion = root / "completion.json"
            prompt = root / "prompt"
            prompt.write_text("stop")
            spec = root / "spec.json"
            spec.write_text(json.dumps({
                "command": ["codex"], "workspace": str(root),
                "prompt_path": str(prompt), "log_path": str(root / "log"),
                "result_path": str(root / "result"), "completion_path": str(completion),
            }))

            def terminate_runner(*_args, **_kwargs):
                os.kill(os.getpid(), signal.SIGTERM)

            with patch("omcp.runner.canonical_codex_executable", return_value="/codex"), \
                    patch("omcp.runner.subprocess.run", side_effect=terminate_runner):
                self.assertEqual(run_worker(spec), 128 + signal.SIGTERM)
            record = json.loads(completion.read_text())
            self.assertEqual(record["exit_code"], 128 + signal.SIGTERM)
            self.assertEqual(record["runner_error"], "RunnerTerminated")

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
                "git_read_paths": [str(root / "git-meta")],
                "execution_capabilities": {
                    "profile": "routine_development_v1", "writable_paths": ["src"],
                    "network": False, "external_credentials": False,
                    "git_metadata": "read_only",
                },
            }))
            with patch.dict(os.environ, codex_fixture(root), clear=True), \
                    patch("omcp.runner.subprocess.run",
                          side_effect=codex_success_side_effect(root / "result")) as execute:
                self.assertEqual(run_worker(spec), 0)
            command = execute.call_args.args[0]
            self.assertNotIn("--sandbox", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--ignore-rules", command)
            self.assertIn("--ephemeral", command)
            self.assertIn("permissions.orchestra-routine.network.enabled=false", command)
            self.assertIn(
                'permissions.orchestra-routine.filesystem./etc/orchestra="deny"',
                command,
            )
            self.assertIn(
                f'permissions.orchestra-routine.filesystem.{root / "git-meta"}="read"',
                command,
            )
            self.assertIn(
                'permissions.orchestra-routine.filesystem.:workspace_roots={"."="read",".orchestra-tmp"="write","src"="write"}',
                command,
            )
            self.assertIn('".orchestra-tmp"="write"', " ".join(command))
            self.assertFalse((root / ".orchestra-tmp").exists())
            self.assertEqual(execute.call_args.kwargs["env"]["TMPDIR"], str(root / ".orchestra-tmp"))

    def test_standing_runner_supports_exact_file_grants_and_completion_contract(self):
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
                "git_read_paths": [],
                "execution_capabilities": {
                    "profile": "routine_development_v1", "writable_paths": [],
                    "writable_files": ["dashboard/system.py", "dashboard/app.py"],
                    "network": False, "external_credentials": False,
                    "git_metadata": "read_only",
                },
            }))
            with patch.dict(os.environ, codex_fixture(root), clear=True), \
                    patch("omcp.runner.subprocess.run",
                          side_effect=codex_success_side_effect(root / "result")) as execute:
                self.assertEqual(run_worker(spec), 0)
            command = execute.call_args.args[0]
            self.assertIn(
                'permissions.orchestra-routine.filesystem.:workspace_roots={'
                '"."="read",".orchestra-tmp"="write",'
                '"dashboard/system.py"="write","dashboard/app.py"="write"}',
                command,
            )
            self.assertIn('".orchestra-tmp"="write"', " ".join(command))
            self.assertNotIn('"."="write"', " ".join(command))
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
                with self.assertRaisesRegex(ValueError, "exactly one executable"):
                    run_worker(spec)
                execute.assert_not_called()


class HostOperationRunnerTests(unittest.TestCase):
    def test_docker_gate_allows_only_cached_disposable_internal_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            approved = root / "workspace"
            approved.mkdir()
            mount_stage = state / "staged-mounts"
            mount_stage.mkdir()
            candidate = state / "candidate"
            candidate.mkdir()
            environment = {
                "OMCP_DOCKER_REAL": "/bin/echo",
                "OMCP_DOCKER_PROJECT_DIR": str(root / "runtime"),
                "OMCP_DOCKER_PROJECT": "omcp-dzn-test",
                "OMCP_DOCKER_COMPOSE_FILE": str(root / "runtime/compose.yaml"),
                "OMCP_DOCKER_OVERRIDE": str(root / "override.yaml"),
                "OMCP_DOCKER_RUNTIME_STATE": str(state),
                "OMCP_DOCKER_NETWORK": "omcp-dzn-test_runtime",
                "OMCP_DOCKER_APPROVED_SOURCE": str(approved),
                "OMCP_DOCKER_MOUNT_STAGE": str(mount_stage),
            }
            compose = [
                "compose", "--project-directory", environment["OMCP_DOCKER_PROJECT_DIR"],
                "--project-name", "omcp-dzn-test", "-f",
                environment["OMCP_DOCKER_COMPOSE_FILE"],
                "up", "--pull", "never", "-d", "--wait", "db",
            ]
            gated = gated_command(compose, environment)
            self.assertIn(environment["OMCP_DOCKER_OVERRIDE"], gated)
            run = gated_command([
                "run", "--rm", "--pull=never", "-v",
                f"{candidate}:{candidate}:ro", "--entrypoint", "php",
                "wordpress:cli-php8.3", str(candidate / "tests/static.php"),
            ], environment)
            self.assertIn("none", run)
            with self.assertRaisesRegex(PermissionError, "outside"):
                gated_command(["pull", "wordpress:latest"], environment)
            with self.assertRaisesRegex(PermissionError, "mounts"):
                gated_command([
                    "run", "--rm", "--pull=never", "-v", "/:/host",
                    "--entrypoint", "php", "wordpress:cli-php8.3", "-v",
                ], environment)

    def test_docker_gate_stages_only_approved_read_only_external_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            stage = state / "staged-mounts"
            stage.mkdir()
            approved = root / "workspace"
            source = approved / "platform"
            fixture = source / "tests/schema30.php"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("<?php echo 'schema30';\n")
            schema30_source = state / "schema30-source"
            schema30_source.symlink_to(source, target_is_directory=True)
            outside = root / "outside.php"
            outside.write_text("secret\n")
            environment = {
                "OMCP_DOCKER_REAL": "/bin/echo",
                "OMCP_DOCKER_PROJECT_DIR": str(approved / "platform/runtime"),
                "OMCP_DOCKER_PROJECT": "omcp-dzn-test",
                "OMCP_DOCKER_COMPOSE_FILE": str(approved / "platform/runtime/compose.yaml"),
                "OMCP_DOCKER_OVERRIDE": str(root / "override.yaml"),
                "OMCP_DOCKER_RUNTIME_STATE": str(state),
                "OMCP_DOCKER_NETWORK": "omcp-dzn-test_runtime",
                "OMCP_DOCKER_APPROVED_SOURCE": str(approved),
                "OMCP_DOCKER_MOUNT_STAGE": str(stage),
            }
            gated = gated_command([
                "run", "--rm", "--pull=never", "-v", f"{schema30_source}:/plugin:ro",
                "--entrypoint", "php", "wordpress:cli-php8.3", "/plugin/tests/schema30.php",
            ], environment)
            staged_volume = gated[gated.index("-v") + 1]
            staged_source = Path(staged_volume.split(":", 1)[0])
            self.assertTrue(staged_source.is_relative_to(stage.resolve()))
            self.assertEqual(
                (staged_source / "tests/schema30.php").read_text(), fixture.read_text(),
            )
            self.assertEqual(staged_volume.split(":", 1)[1], "/plugin:ro")

            for denied in (f"{schema30_source}:/plugin", f"{outside}:/fixture.php:ro"):
                with self.subTest(denied=denied), self.assertRaises(PermissionError):
                    gated_command([
                        "run", "--rm", "--pull=never", "-v", denied,
                        "--entrypoint", "php", "wordpress:cli-php8.3", "/fixture.php",
                    ], environment)

    def test_host_runner_uses_pinned_scripts_minimal_environment_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            runtime = workspace / "platform/runtime"
            scripts = runtime / "bin"
            scripts.mkdir(parents=True)
            (runtime / "compose.yaml").write_text("services: {}\n")
            (scripts / "run-all.sh").write_text("#!/bin/bash\nexit 0\n")
            (scripts / "destroy.sh").write_text("#!/bin/bash\nexit 0\n")
            task_dir = root / "task"
            task_dir.mkdir()
            spec = task_dir / "host-operation.json"
            completion = task_dir / "completion.json"
            lease = task_dir / "lease.json"
            spec.write_text(json.dumps({
                "operation_id": "delnavazan-mariadb",
                "operation_type": "delnavazan_mariadb_acceptance_v1",
                "target_commit": "1" * 40, "harness_commit": "2" * 40,
                "timeout_seconds": 600, "workspace": str(workspace),
                "task_dir": str(task_dir), "log_path": str(task_dir / "operation.log"),
                "result_path": str(task_dir / "result.txt"),
                "completion_path": str(completion),
                "task_id": "11111111-1111-1111-1111-111111111111",
                "lease_id": "22222222-2222-2222-2222-222222222222",
                "lease_path": str(lease),
            }))
            parent = {"HOME": "/Users/test", "OPENAI_API_KEY": "must-not-pass"}
            with patch.dict(os.environ, parent, clear=True), \
                    patch("omcp.host_runner.shutil.which", return_value="/bin/echo"), \
                    patch("omcp.host_runner._free_loopback_port", return_value=23456), \
                    patch("omcp.host_runner.subprocess.run", side_effect=[
                        Mock(returncode=0), Mock(returncode=0),
                    ]) as execute:
                self.assertEqual(run_host_operation(spec), 0)
            self.assertEqual(len(execute.call_args_list), 2)
            first_environment = execute.call_args_list[0].kwargs["env"]
            self.assertNotIn("OPENAI_API_KEY", first_environment)
            self.assertEqual(first_environment["HOME"], str(task_dir.resolve() / "home"))
            self.assertEqual(first_environment["DZN_TARGET_REF"], "1" * 40)
            self.assertEqual(first_environment["DZN_WP_PORT"], "23456")
            self.assertEqual(first_environment["OMCP_DOCKER_APPROVED_SOURCE"], str(workspace.resolve()))
            self.assertEqual(
                first_environment["OMCP_DOCKER_MOUNT_STAGE"],
                str(task_dir.resolve() / "runtime-state/staged-mounts"),
            )
            self.assertFalse((task_dir / "runtime-state/staged-mounts").exists())
            self.assertEqual(
                (task_dir / "compose.internal.yaml").read_text(),
                "networks:\n  runtime:\n    internal: true\n",
            )
            lease_record = json.loads(lease.read_text())
            self.assertEqual(lease_record["task_id"], "11111111-1111-1111-1111-111111111111")
            self.assertEqual(lease_record["lease_id"], "22222222-2222-2222-2222-222222222222")
            self.assertEqual(json.loads(completion.read_text())["exit_code"], 0)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.controller = Mock()
        self.controller.bridge_status.return_value = {"status": "ok"}
        self.server = MCPServer(self.controller)

    def test_initialize_and_tool_discovery(self):
        session = self.server.new_session()
        initialized = session.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "orchestra-omcp")
        session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = session.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertEqual({tool["name"] for tool in TOOLS},
                         {tool["name"] for tool in listed["result"]["tools"]})

    def test_tool_call_returns_structured_content(self):
        session = self.server.new_session()
        session.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = session.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "bridge_status", "arguments": {}},
        })
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"], {"status": "ok"})

    def test_unknown_task_returns_structured_instance_error(self):
        identity = {
            "instance_id": "11111111-1111-1111-1111-111111111111",
            "state_store_id": "a" * 24,
            "instance_role": "authoritative",
        }
        self.controller.public_identity.return_value = identity
        self.controller.task_status.side_effect = UnknownTaskError(
            "22222222-2222-2222-2222-222222222222", self.controller,
        )
        session = self.server.new_session()
        session.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = session.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "task_status", "arguments": {
                "task_id": "22222222-2222-2222-2222-222222222222",
            }},
        })
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(
            response["result"]["structuredContent"],
            {"error": "task_not_found_in_instance", **identity},
        )

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

    def test_disabled_mode_does_not_start_local_approval_server(self):
        self.controller.config.task_approval_mode = "disabled"
        with patch("omcp.server.LocalApprovalServer") as approval_server:
            local_approval, ticker = self.server._start_auxiliary()
            self.server._stop_auxiliary(local_approval, ticker)
        self.assertIsNone(local_approval)
        approval_server.assert_not_called()

    def test_unknown_method_uses_json_rpc_error(self):
        session = self.server.new_session()
        session.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = session.handle({"jsonrpc": "2.0", "id": 4, "method": "unknown"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_handshake_rejects_unsupported_version_and_pre_ready_requests(self):
        session = self.server.new_session()
        early = session.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(early["error"]["code"], -32002)
        unsupported = session.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2027-01-01"}})
        self.assertEqual(unsupported["error"]["code"], -32602)

    def test_current_tunnel_initialize_uses_the_normal_mcp_handshake(self):
        session = self.server.new_session()
        initialized = session.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "secure-mcp-tunnel", "version": "test"},
            },
        })
        self.assertEqual(initialized["result"]["protocolVersion"], "2026-07-28")
        self.assertEqual(session.state, session.NEGOTIATED)
        session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = session.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertEqual({tool["name"] for tool in TOOLS},
                         {tool["name"] for tool in listed["result"]["tools"]})

    def test_current_tunnel_initialize_with_stateless_metadata_is_not_dispatched_as_a_tool(self):
        session = self.server.new_session()
        initialized = session.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": self.stateless_params(
                protocolVersion="2026-07-28", capabilities={},
                clientInfo={"name": "secure-mcp-tunnel", "version": "test"},
            ),
        })
        self.assertEqual(initialized["result"]["protocolVersion"], "2026-07-28")
        self.assertEqual(session.state, session.NEGOTIATED)

    def test_legacy_reconnect_requires_new_handshake(self):
        first = self.server.new_session()
        first.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        first.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIn("result", first.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
        second = self.server.new_session()
        self.assertEqual(second.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})["error"]["code"], -32002)

    @staticmethod
    def stateless_params(**params):
        return {
            **params,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        }

    def test_stateless_discovery_and_tool_call_need_no_handshake(self):
        session = self.server.new_session()
        discovered = session.handle({
            "jsonrpc": "2.0", "id": 1, "method": "server/discover",
            "params": self.stateless_params(),
        })
        self.assertIn("2026-07-28", discovered["result"]["supportedVersions"])
        listed = session.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            "params": self.stateless_params(),
        })
        self.assertEqual({tool["name"] for tool in TOOLS},
                         {tool["name"] for tool in listed["result"]["tools"]})
        response = session.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": self.stateless_params(name="bridge_status", arguments={}),
        })
        self.assertEqual(response["result"]["structuredContent"], {"status": "ok"})
        self.assertEqual(session.state, session.CONNECTED)

    def test_stateless_reconnect_needs_no_shared_session(self):
        for request_id in (1, 2):
            session = self.server.new_session()
            response = session.handle({
                "jsonrpc": "2.0", "id": request_id, "method": "tools/list",
                "params": self.stateless_params(),
            })
            self.assertIn("result", response)
            self.assertEqual(session.state, session.CONNECTED)

    def test_stateless_path_rejects_incomplete_or_wrong_metadata(self):
        cases = (
            {},
            {"_meta": {}},
            {"_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            }},
            {"_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": [],
            }},
            {"_meta": {
                "io.modelcontextprotocol/protocolVersion": "2027-01-01",
                "io.modelcontextprotocol/clientCapabilities": {},
            }},
        )
        for request_id, params in enumerate(cases, start=1):
            with self.subTest(params=params):
                session = self.server.new_session()
                response = session.handle({
                    "jsonrpc": "2.0", "id": request_id, "method": "tools/list",
                    "params": params,
                })
                self.assertEqual(response["error"]["code"], -32002)


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
        self.server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
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
        description = schema["properties"]["consequence_class"]["description"]
        self.assertIn("eligible routine_development only", description)
        self.assertIn("unclassified work still require explicit approval", description)
        self.assertNotIn("automatically authorizes all valid classes", description)
        self.assertEqual(set(schema["properties"]["consequence_class"]["enum"]), {
            "routine_development", "production_deployment", "destructive_operation",
            "security_change", "unclassified",
        })
        host_operation = next(tool for tool in TOOLS if tool["name"] == "submit_host_operation")
        host_schema = host_operation["inputSchema"]
        self.assertEqual(
            set(host_schema["required"]), {"request_id", "operation_id", "target_commit"},
        )
        self.assertNotIn("command", host_schema["properties"])
        self.assertNotIn("arguments", host_schema["properties"])
        self.assertNotIn("requires_approval", schema["properties"])

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
        self.server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

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
        queued = self.call("task_snapshot", {"recent_limit": 20})
        self.assertEqual(queued["counts"]["queued"], 1)
        self.assertEqual(queued["counts"]["approval_pending"], 1)
        self.assertEqual(queued["queued"][0]["queue_state"], "approval_pending")
        self.assertEqual(queued["recent"], [])
        self.assertEqual(len(self.call("list_approvals")["approvals"]), 1)
        self.assertTrue(self.call("worker_availability")["workers"][0]["available"])
        self.assertEqual(self.call("cancel_task", {
            "task_id": task_id, "reason": "Requester withdrew it",
        })["status"], "cancelled")
        terminal = self.call("task_snapshot", {"recent_limit": 20})
        self.assertEqual(terminal["counts"]["queued"], 0)
        self.assertEqual(terminal["counts"]["recent"], 1)
        self.assertEqual(terminal["recent"][0]["task_id"], task_id)
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
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "bridge_status", "arguments": {}}},
            ]) + "\n"
            environment = {
                **codex_fixture(Path(directory)),
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
            self.assertEqual(len(responses[1]["result"]["tools"]), len(TOOLS))
            self.assertEqual(responses[2]["result"]["structuredContent"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
