import base64
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
import warnings
import zipfile
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

from omcp.github_actions import (
    GitHubActionsClient,
    GitHubActionsError,
    _SafeRedirectHandler,
    decode_instructions,
    encode_instructions,
)
from omcp.github_runner import run as run_github_worker
from scripts.orchestra_actions_worker import (
    decode_payload,
    main as worker_main,
    redact_file,
)


TASK_ID = "11111111-1111-1111-1111-111111111111"


class Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self, _limit: int = -1) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class PayloadHardeningTests(unittest.TestCase):
    def test_encode_rejects_compressible_input_over_uncompressed_limit(self):
        with self.assertRaisesRegex(ValueError, "too large"):
            encode_instructions("A" * 65_537)

    def test_bounded_decoders_reject_a_small_compressed_bomb(self):
        encoded = base64.urlsafe_b64encode(zlib.compress(b"A" * 1_000_000)).decode()
        with self.assertRaisesRegex(ValueError, "bound"):
            decode_instructions(encoded)
        with self.assertRaises(SystemExit):
            decode_payload(encoded)

    def test_decoders_reject_trailing_or_non_base64_data(self):
        valid = base64.urlsafe_b64encode(zlib.compress(b"task")).decode()
        trailing = base64.urlsafe_b64encode(zlib.compress(b"task") + b"junk").decode()
        for encoded in (valid + "!", trailing):
            with self.subTest(encoded=encoded[-12:]):
                with self.assertRaises(ValueError):
                    decode_instructions(encoded)
                with self.assertRaises(SystemExit):
                    decode_payload(encoded)


class HttpHardeningTests(unittest.TestCase):
    def test_cross_origin_redirect_drops_authorization(self):
        request = urllib.request.Request(
            "https://api.github.com/artifact",
            headers={"Authorization": "Bearer secret"},
        )
        redirected = _SafeRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://objects.example/artifact",
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_redirect_refuses_https_downgrade(self):
        request = urllib.request.Request("https://api.github.com/artifact")
        with self.assertRaises(urllib.error.HTTPError):
            _SafeRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, "http://objects.example/artifact",
            )

    def test_client_rejects_malformed_task_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("secret-token\n")
            token.chmod(0o600)
            client = GitHubActionsClient(
                "owner/repo", "orchestra-worker.yml", "main", token,
                opener=Mock(),
            )
            with self.assertRaisesRegex(ValueError, "task_id"):
                client.dispatch("-" * 36, "request-1", "codex", "gpt-5", "task")


class ArtifactHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.token = Path(self.temp.name) / "token"
        self.token.write_text("secret-token\n")
        self.token.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def client_for_archive(self, archive: bytes) -> GitHubActionsClient:
        metadata = json.dumps({"artifacts": [{
            "name": "omcp-" + TASK_ID,
            "archive_download_url": (
                "https://api.github.com/repos/owner/repo/actions/artifacts/1/zip"
            ),
        }]}).encode()
        responses = iter((metadata, archive))
        return GitHubActionsClient(
            "owner/repo", "orchestra-worker.yml", "main", self.token,
            opener=lambda *_args, **_kwargs: Response(next(responses)),
        )

    def test_artifact_rejects_duplicate_allowed_names(self):
        buffer = io.BytesIO()
        with warnings.catch_warnings(), zipfile.ZipFile(buffer, "w") as archive:
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("result.txt", "first")
            archive.writestr("result.txt", "second")
        with self.assertRaisesRegex(GitHubActionsError, "duplicate"):
            self.client_for_archive(buffer.getvalue()).download_artifact(
                1, "omcp-" + TASK_ID,
            )

    def test_artifact_rejects_excessive_aggregate_uncompressed_size(self):
        buffer = io.BytesIO()
        content = b"A" * (8 * 1024 * 1024 + 1)
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("result.txt", content)
            archive.writestr("patch.diff", content)
        with self.assertRaisesRegex(GitHubActionsError, "artifact is too large"):
            self.client_for_archive(buffer.getvalue()).download_artifact(
                1, "omcp-" + TASK_ID,
            )


class RunnerHardeningTests(unittest.TestCase):
    def test_pre_cancelled_task_is_not_dispatched(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)
            workspace = task / "workspace"
            workspace.mkdir()
            (task / "cancel.json").write_text("{}\n")
            spec_path = task / "github-actions.json"
            spec_path.write_text(json.dumps({
                "task_id": TASK_ID,
                "prompt_path": str(task / "prompt.txt"),
                "log_path": str(task / "worker.log"),
                "completion_path": str(task / "completion.json"),
                "result_path": str(task / "result.txt"),
                "workspace": str(workspace),
                "lease_id": "22222222-2222-2222-2222-222222222222",
                "lease_path": str(task / "lease.json"),
            }))
            client = Mock()
            client.find_run.return_value = None
            with patch("omcp.github_runner.client_from_spec", return_value=client):
                self.assertEqual(run_github_worker(spec_path), 130)
            client.dispatch.assert_not_called()
            self.assertEqual(
                json.loads((task / "completion.json").read_text())["runner_error"],
                "Cancelled",
            )


class WorkerArtifactTests(unittest.TestCase):
    def test_artifact_text_is_bounded_and_exact_secrets_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.log"
            path.write_text("before super-secret-token after" + "x" * 100)
            redact_file(path, ("super-secret-token",), 40)
            text = path.read_text()
        self.assertNotIn("super-secret-token", text)
        self.assertIn("[REDACTED]", text)
        self.assertLessEqual(len(text.encode()), 40)

    def test_codex_receives_selected_model_but_git_never_receives_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            runtime = root / "runtime"
            encoded = base64.urlsafe_b64encode(zlib.compress(b"make a change")).decode()
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(root / "home"),
                "ORCHESTRA_TASK_ID": TASK_ID,
                "ORCHESTRA_REQUEST_ID": "request-1",
                "ORCHESTRA_AGENT": "codex",
                "ORCHESTRA_MODEL": "gpt-5-codex",
                "ORCHESTRA_PAYLOAD": encoded,
                "ORCHESTRA_OUTPUT_DIR": str(output),
                "ORCHESTRA_RUNTIME_DIR": str(runtime),
                "OPENAI_API_KEY": "openai-secret-token-value",
                "DEEPSEEK_API_KEY": "deepseek-must-not-pass",
            }
            completed = [
                __import__("subprocess").CompletedProcess(["codex"], 0),
                __import__("subprocess").CompletedProcess(["git", "add"], 0, stdout=""),
                __import__("subprocess").CompletedProcess(["git", "diff"], 0, stdout=b""),
            ]
            previous = Path.cwd()
            os.chdir(root)
            try:
                with patch.dict(os.environ, environment, clear=True), patch(
                    "scripts.orchestra_actions_worker.subprocess.run",
                    side_effect=completed,
                ) as run:
                    self.assertEqual(worker_main(), 0)
            finally:
                os.chdir(previous)
        agent_command = run.call_args_list[0].args[0]
        self.assertEqual(
            agent_command[agent_command.index("--model") + 1], "gpt-5-codex",
        )
        self.assertIn("shell_environment_policy.inherit=core", agent_command)
        self.assertIn(
            "shell_environment_policy.ignore_default_excludes=false", agent_command,
        )
        self.assertIn("sandbox_workspace_write.network_access=false", agent_command)
        self.assertEqual(
            run.call_args_list[0].kwargs["env"]["OPENAI_API_KEY"],
            "openai-secret-token-value",
        )
        self.assertNotIn("DEEPSEEK_API_KEY", run.call_args_list[0].kwargs["env"])
        for call in run.call_args_list[1:]:
            self.assertNotIn("OPENAI_API_KEY", call.kwargs["env"])
            self.assertNotIn("DEEPSEEK_API_KEY", call.kwargs["env"])

    def test_workflow_actions_are_pinned_to_full_commit_shas(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/orchestra-worker.yml"
        ).read_text()
        for action in ("actions/checkout", "actions/setup-python", "actions/upload-artifact"):
            self.assertRegex(workflow, action + r"@[0-9a-f]{40}")
        self.assertNotRegex(workflow, r"uses: actions/[^@]+@v\d")

    def test_workflow_scopes_each_provider_secret_to_its_agent_step(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/orchestra-worker.yml"
        ).read_text()
        codex_step, deepseek_step = workflow.split(
            "- name: Execute approved Orchestra task with DeepSeek", 1,
        )
        codex_step = codex_step.rsplit(
            "- name: Execute approved Orchestra task with Codex", 1,
        )[1]
        deepseek_step = deepseek_step.split(
            "- name: Upload bounded result and patch", 1,
        )[0]
        self.assertIn("if: inputs.agent == 'codex'", codex_step)
        self.assertIn("OPENAI_API_KEY", codex_step)
        self.assertNotIn("DEEPSEEK_API_KEY", codex_step)
        self.assertIn("if: inputs.agent == 'deepseek'", deepseek_step)
        self.assertIn("DEEPSEEK_API_KEY", deepseek_step)
        self.assertNotIn("OPENAI_API_KEY", deepseek_step)


if __name__ == "__main__":
    unittest.main()
