import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

from mac_bridge.bridge import Bridge, Config, Worker


def issue(number=7, worker="codex", author="hamed"):
    return {"number": number, "title": "Bridge task", "body": "Inspect the project", "user": {"login": author},
            "labels": [{"name": "bridge-approved"}, {"name": f"bridge-worker:{worker}"}]}


class FakeProcess:
    pid = 12345

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.code = None

    def poll(self):
        return self.code


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.state = Path(self.temp.name) / "state"
        self.workers = {name: Worker(name, ("codex",)) for name in ("codex", "dzn-ina", "dzn-deepseek")}
        self.config = Config("owner/repo", self.root, self.state, self.workers, approved_authors=frozenset({"hamed"}))
        self.github = Mock()
        self.github.issues.return_value = [issue()]
        self.popen = Mock(side_effect=FakeProcess)
        self.bridge = Bridge(self.config, self.github, self.popen)
        self.bridge._prepare_workspace = Mock(return_value=self.root)
        def fake_git(_directory, *args, **_kwargs):
            if args[0] == "status":
                return 0, ""
            if args[0] == "rev-parse":
                return 0, "base-sha"
            if args[0] == "branch":
                return 0, "bridge/issue-7"
            if args[0] == "remote":
                return 0, "https://github.com/owner/repo.git"
            return 0, ""
        self.bridge._git = Mock(side_effect=fake_git)

    def tearDown(self):
        self.bridge.lock_file.close()
        self.temp.cleanup()

    def test_claims_authorized_issue_and_records_status(self):
        self.assertEqual(self.bridge.intake_once(), 1)
        proc = self.bridge.children[12345][0]
        self.assertIn("--sandbox", proc.args[0])
        self.assertIn("workspace-write", proc.args[0])
        self.assertEqual(proc.kwargs["cwd"], self.root)
        self.assertTrue(proc.kwargs["stdin"].closed)
        prompt = (self.state / "issue-7.prompt.txt").read_text()
        self.assertIn("GitHub issue #7: Bridge task", prompt)
        self.assertIn("commit the completed changes on this branch", prompt)
        self.assertIn("Exit 0 reports execution completion only", prompt)
        self.assertEqual(self.bridge.state["tasks"]["7"]["status"], "running")

    def test_unapproved_author_never_launches(self):
        self.github.issues.return_value = [issue(author="stranger")]
        self.assertEqual(self.bridge.intake_once(), 0)
        self.popen.assert_not_called()
        self.github.add_label.assert_not_called()

    def test_multiple_worker_labels_are_not_launched(self):
        candidate = issue()
        candidate["labels"].append({"name": "bridge-worker:dzn-ina"})
        self.github.issues.return_value = [candidate]
        self.assertEqual(self.bridge.intake_once(), 0)
        self.popen.assert_not_called()

    def test_only_one_worker_uses_workspace(self):
        self.github.issues.return_value = [issue(7), issue(8, "dzn-ina")]
        self.assertEqual(self.bridge.intake_once(), 1)
        self.popen.assert_called_once()

    def test_oversized_issue_stays_queued(self):
        candidate = issue()
        candidate["body"] = "x" * 65537
        self.github.issues.return_value = [candidate]
        self.assertEqual(self.bridge.intake_once(), 0)
        self.popen.assert_not_called()
        self.github.comment.assert_called_once()

    def test_rejects_approval_bypassing_worker_config(self):
        config_path = self.state / "config.json"
        raw = {"repository": "owner/repo", "project_root": str(self.root), "state_dir": str(self.state),
               "workers": {name: {"command": (["codex", "--approve-for-me"] if name == "dzn-deepseek" else ["codex"])}
                           for name in ("codex", "dzn-ina", "dzn-deepseek")}}
        config_path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, "bypasses approval"):
            Config.load(config_path)

    def test_rejects_worker_shell_wrappers(self):
        config_path = self.state / "config.json"
        raw = {"repository": "owner/repo", "project_root": str(self.root), "state_dir": str(self.state),
               "workers": {name: {"command": (["dzn-deepseek"] if name == "dzn-deepseek" else ["codex"])}
                           for name in ("codex", "dzn-ina", "dzn-deepseek")}}
        config_path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, "directly"):
            Config.load(config_path)

    def test_recovery_marks_missing_worker_interrupted_without_retry(self):
        self.bridge.state["tasks"]["7"] = {"status": "running", "pid": 989898, "worker": "codex"}
        self.bridge._save()
        with patch("mac_bridge.bridge.os.kill", side_effect=ProcessLookupError):
            self.assertFalse(self.bridge._workspace_busy())
        self.assertEqual(self.bridge.state["tasks"]["7"]["status"], "interrupted")
        self.github.remove_label.assert_called_once_with(7, "bridge-claimed")
        self.popen.assert_not_called()

    def test_completion_publishes_bounded_result_but_not_output(self):
        self.bridge.intake_once()
        proc, log_path, _, _ = self.bridge.children[12345]
        log_path.write_text("worker output including private details")
        proc.code = 0
        self.bridge.reap()
        self.bridge.publish_terminal_states()
        self.assertEqual(self.bridge.state["tasks"]["7"]["status"], "completed")
        comments = [call.args[1] for call in self.github.comment.call_args_list]
        self.assertEqual(len(comments), 2)
        self.assertTrue(any('"execution_exit_status":0' in value for value in comments))
        self.assertTrue(all("private details" not in value for value in comments))

    def _complete_worker(self, exit_code=0, git_outputs=None):
        self.bridge.intake_once()
        proc, _, _, _ = self.bridge.children[12345]
        proc.code = exit_code
        if git_outputs is not None:
            self.bridge._git.side_effect = git_outputs
        self.bridge.reap()
        return self.bridge.state["tasks"]["7"]["result"]

    def test_result_no_changes_is_durable_and_not_task_success(self):
        result = self._complete_worker()
        self.assertFalse(result["files_changed"])
        self.assertEqual(result["delivery"], "no_changes")
        self.assertIn("no commit or file changes", result["blocker"])
        self.assertTrue((self.state / "issue-7.result.json").is_file())
        self.assertEqual(result["execution_exit_status"], 0)

    def test_result_changed_but_uncommitted_is_blocked(self):
        result = self._complete_worker(git_outputs=[(0, " M file.php"), (0, "base-sha")])
        self.assertTrue(result["files_changed"])
        self.assertIn("uncommitted", result["blocker"])
        self.assertNotIn("push", [call.args[1] for call in self.bridge._git.call_args_list])

    def test_committed_result_is_pushed_and_branch_reported(self):
        result = self._complete_worker(git_outputs=[(0, ""), (0, "commit-sha"), (0, "bridge/issue-7"),
                                                     (0, "https://github.com/owner/repo.git"), (0, "")])
        self.assertTrue(result["files_changed"])
        self.assertEqual(result["commit_sha"], "commit-sha")
        self.assertEqual(result["delivery"], "delivered")
        self.assertEqual(result["delivered_branch"], "bridge/issue-7")

    def test_push_failure_has_actionable_blocker(self):
        result = self._complete_worker(git_outputs=[(0, ""), (0, "commit-sha"), (0, "bridge/issue-7"),
                                                     (0, "https://github.com/owner/repo.git"), (128, "")])
        self.assertEqual(result["delivery"], "blocked")
        self.assertIn("GitHub write credentials", result["blocker"])

    def test_worker_failure_is_not_pushed(self):
        result = self._complete_worker(exit_code=2, git_outputs=[(0, " M file.php"), (0, "base-sha")])
        self.assertEqual(result["execution_exit_status"], 2)
        self.assertIn("worker exited", result["blocker"])
        self.assertFalse(any("push" in call.args[1:] for call in self.bridge._git.call_args_list))

    def test_explicit_reapproval_creates_a_new_attempt(self):
        self.bridge.state["tasks"]["7"] = {"status": "interrupted", "attempt": 1, "worker": "codex", "exit_code": None}
        self.github.issues.return_value = [issue()]
        self.assertEqual(self.bridge.intake_once(), 1)
        current = self.bridge.state["tasks"]["7"]
        self.assertEqual(current["attempt"], 2)
        self.assertEqual(current["history"][0]["status"], "interrupted")

    def test_terminal_github_update_retries_after_api_failure(self):
        self.bridge.state["tasks"]["7"] = {"status": "succeeded", "worker": "codex", "exit_code": 0,
                                             "reported": False}
        self.github.comment.side_effect = [RuntimeError("offline"), None]
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.bridge.publish_terminal_states()
        self.assertFalse(self.bridge.state["tasks"]["7"].get("commented", False))
        self.bridge.publish_terminal_states()
        self.assertTrue(self.bridge.state["tasks"]["7"]["reported"])

    def test_config_rejects_state_inside_worker_checkout(self):
        config_path = self.state / "config.json"
        raw = {"repository": "owner/repo", "project_root": str(self.root), "state_dir": str(self.root / "state"),
               "workers": {name: {"command": ["codex"]} for name in ("codex", "dzn-ina", "dzn-deepseek")}}
        config_path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, "outside the worker project root"):
            Config.load(config_path)

    def test_new_issue_workspace_fetches_default_and_creates_isolated_branch(self):
        del self.bridge._prepare_workspace
        with patch.object(self.bridge, "_git", side_effect=[
            (0, "https://github.com/owner/repo.git"), (0, ""), (1, ""), (0, ""), (0, "")
        ]) as git:
            path = self.bridge._prepare_workspace(7, "bridge/issue-7", None)
        self.assertEqual(path, self.state / "worktrees" / "issue-7")
        calls = [call.args for call in git.call_args_list]
        self.assertEqual(calls[1][1:], ("fetch", "--no-tags", "origin", "main"))
        self.assertEqual(calls[3][1:], ("worktree", "add", "--detach", str(path), "origin/main"))
        self.assertEqual(calls[4][1:], ("switch", "-c", "bridge/issue-7"))

    def test_reapproved_attempt_reuses_its_existing_worktree(self):
        path = self.state / "worktrees" / "issue-7"
        path.mkdir(parents=True)
        previous = {"workspace_path": str(path)}
        del self.bridge._prepare_workspace
        with patch.object(self.bridge, "_git", side_effect=[(0, str(self.root / ".git")),
                                                              (0, str(self.root / ".git")),
                                                              (0, "bridge/issue-7")]) as git:
            result = self.bridge._prepare_workspace(7, "bridge/issue-7", previous)
        self.assertEqual(result, path)
        self.assertEqual(git.call_count, 3)


if __name__ == "__main__":
    unittest.main()
