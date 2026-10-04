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

    def test_completion_publishes_status_but_not_output(self):
        self.bridge.intake_once()
        proc, log_path, _, _ = self.bridge.children[12345]
        log_path.write_text("worker output including private details")
        proc.code = 0
        self.bridge.reap()
        self.assertEqual(self.bridge.state["tasks"]["7"]["status"], "succeeded")
        comments = [call.args[1] for call in self.github.comment.call_args_list]
        self.assertEqual(len(comments), 2)
        self.assertTrue(any("finished with exit code 0" in value for value in comments))
        self.assertTrue(all("private details" not in value for value in comments))

    def test_config_rejects_state_inside_worker_checkout(self):
        config_path = self.state / "config.json"
        raw = {"repository": "owner/repo", "project_root": str(self.root), "state_dir": str(self.root / "state"),
               "workers": {name: {"command": ["codex"]} for name in ("codex", "dzn-ina", "dzn-deepseek")}}
        config_path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, "outside the worker project root"):
            Config.load(config_path)


if __name__ == "__main__":
    unittest.main()
