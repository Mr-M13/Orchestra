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
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from omcp.github_actions import (
    GitHubActionsClient,
    GitHubActionsError,
    DispatchIdentity,
    _SafeRedirectHandler,
    decode_instructions,
    encode_instructions,
)
from omcp.github_runner import run as run_github_worker
from omcp.github_dispatch_service import DispatchService, ServiceConfig
from scripts.orchestra_actions_worker import (
    decode_payload,
    main as worker_main,
    redact_file,
)


TASK_ID = "11111111-1111-1111-1111-111111111111"
TASK_SHA = "a" * 40
DISPATCH_ID = "22222222-2222-4222-8222-222222222222"
DISPATCHED_AT = "2026-10-20T00:00:00+00:00"


def dispatch_identity() -> DispatchIdentity:
    return DispatchIdentity(TASK_ID, "request-1", DISPATCH_ID, DISPATCHED_AT)


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
                "owner/repo", "orchestra-worker.yml", "main", TASK_SHA, token,
                opener=Mock(),
            )
            with self.assertRaisesRegex(ValueError, "task_id"):
                client.dispatch(
                    DispatchIdentity("-" * 36, "request-1", DISPATCH_ID, DISPATCHED_AT),
                    "codex", "gpt-5", "task",
                )

    def test_client_requires_current_service_uid_and_private_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("secret-token\n")
            token.chmod(0o600)
            with self.assertRaisesRegex(PermissionError, "identity"):
                GitHubActionsClient(
                    "owner/repo", "orchestra-worker.yml", "main", TASK_SHA,
                    token, service_uid=os.geteuid() + 1,
                )
            Path(directory).chmod(0o750)
            with self.assertRaisesRegex(PermissionError, "directory"):
                GitHubActionsClient(
                    "owner/repo", "orchestra-worker.yml", "main", TASK_SHA, token,
                )

    def test_client_rejects_symlinked_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real-token"
            link = root / "token"
            real.write_text("secret-token\n")
            real.chmod(0o600)
            link.symlink_to(real)
            with self.assertRaisesRegex(PermissionError, "regular file"):
                GitHubActionsClient(
                    "owner/repo", "orchestra-worker.yml", "main", TASK_SHA, link,
                )

    def test_client_rejects_tag_and_raw_sha_workflow_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("secret-token\n")
            token.chmod(0o600)
            for ref in ("refs/tags/reviewed", TASK_SHA, "refs/heads/"):
                with self.subTest(ref=ref), self.assertRaisesRegex(
                    ValueError, "must name a branch",
                ):
                    GitHubActionsClient(
                        "owner/repo", "orchestra-worker.yml", ref, TASK_SHA, token,
                    )

    def test_find_run_rejects_stale_same_name_and_wrong_ref_or_sha(self):
        created = datetime.fromisoformat(DISPATCHED_AT)
        exact = {
            "id": 7, "display_title": dispatch_identity().run_name,
            "event": "workflow_dispatch", "head_sha": TASK_SHA,
            "head_branch": "main", "created_at": created.isoformat(),
            "status": "queued", "conclusion": None,
            "html_url": "https://github.com/owner/repo/actions/runs/7",
        }
        stale = {**exact, "id": 6,
                 "created_at": (created - timedelta(days=1)).isoformat()}
        wrong_sha = {**exact, "id": 5, "head_sha": "b" * 40}
        payload = json.dumps({"workflow_runs": [stale, wrong_sha, exact]}).encode()
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("secret-token\n")
            token.chmod(0o600)
            client = GitHubActionsClient(
                "owner/repo", "orchestra-worker.yml", "main", TASK_SHA, token,
                opener=lambda *_args, **_kwargs: Response(payload),
            )
            run = client.find_run(dispatch_identity())
        self.assertIsNotNone(run)
        self.assertEqual(run.run_id, 7)

    def test_dispatch_fails_closed_for_unprotected_ref_before_post(self):
        requests = []

        def opener(request, **_kwargs):
            requests.append(request)
            return Response(json.dumps({
                "name": "main", "protected": False,
                "commit": {"sha": TASK_SHA},
            }).encode())

        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("secret-token\n")
            token.chmod(0o600)
            client = GitHubActionsClient(
                "owner/repo", "orchestra-worker.yml", "main", TASK_SHA, token,
                opener=opener,
            )
            with self.assertRaisesRegex(GitHubActionsError, "protected branch"):
                client.dispatch(dispatch_identity(), "codex", "gpt-5", "task")
        self.assertEqual([request.method for request in requests], ["GET"])

    def test_dispatch_fails_closed_when_ref_moved_before_post(self):
        requests = []

        def opener(request, **_kwargs):
            requests.append(request)
            return Response(json.dumps({
                "name": "main", "protected": True,
                "commit": {"sha": "b" * 40},
            }).encode())

        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("secret-token\n")
            token.chmod(0o600)
            client = GitHubActionsClient(
                "owner/repo", "orchestra-worker.yml", "main", TASK_SHA, token,
                opener=opener,
            )
            with self.assertRaisesRegex(GitHubActionsError, "configured SHA"):
                client.dispatch(dispatch_identity(), "codex", "gpt-5", "task")
        self.assertEqual([request.method for request in requests], ["GET"])

    def test_dispatch_requires_review_protected_secret_environment(self):
        requests = []
        responses = iter((
            {
                "name": "main", "protected": True,
                "commit": {"sha": TASK_SHA},
            },
            {
                "name": "orchestra-workers",
                "protection_rules": [],
                "deployment_branch_policy": {"protected_branches": True},
            },
        ))

        def opener(request, **_kwargs):
            requests.append(request)
            return Response(json.dumps(next(responses)).encode())

        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("secret-token\n")
            token.chmod(0o600)
            client = GitHubActionsClient(
                "owner/repo", "orchestra-worker.yml", "main", TASK_SHA, token,
                opener=opener,
            )
            with self.assertRaisesRegex(GitHubActionsError, "environment"):
                client.dispatch(dispatch_identity(), "codex", "gpt-5", "task")
        self.assertEqual([request.method for request in requests], ["GET", "GET"])


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
            "owner/repo", "orchestra-worker.yml", "main", TASK_SHA, self.token,
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
                "github_dispatch_socket": str(task / "dispatch.sock"),
                "github_service_uid": os.geteuid() + 1,
                "lease_id": "22222222-2222-2222-2222-222222222222",
                "lease_path": str(task / "lease.json"),
            }))
            client = Mock()
            client.find_run.return_value = None
            with patch("omcp.github_runner.DispatchClient", return_value=client):
                self.assertEqual(run_github_worker(spec_path), 130)
            client.dispatch.assert_not_called()
            self.assertEqual(
                json.loads((task / "completion.json").read_text())["runner_error"],
                "Cancelled",
            )


class DispatchBrokerBoundaryTests(unittest.TestCase):
    def service(self):
        service = object.__new__(DispatchService)
        service.config = SimpleNamespace(
            allowed_models={"codex": frozenset({"gpt-5-codex"})},
        )
        service.github = Mock()
        return service

    def request(self, action, **values):
        return {
            "version": 1, "action": action,
            "identity": {
                "task_id": TASK_ID, "request_id": "request-1",
                "dispatch_id": DISPATCH_ID, "dispatched_at": DISPATCHED_AT,
            },
            **values,
        }

    def test_dispatch_api_rejects_caller_selected_target_fields(self):
        service = self.service()
        request = self.request(
            "dispatch", agent="codex", model="gpt-5-codex",
            instructions="bounded task", github_repository="attacker/repo",
        )
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            service.handle(request)
        service.github.dispatch.assert_not_called()

    def test_dispatch_api_enforces_model_allowlist(self):
        service = self.service()
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            service.handle(self.request(
                "dispatch", agent="codex", model="unreviewed",
                instructions="bounded task",
            ))
        service.github.dispatch.assert_not_called()

    def test_artifact_name_is_derived_from_task_identity(self):
        service = self.service()
        service.github.status.return_value = Mock()
        with self.assertRaisesRegex(ValueError, "not bound to this task"):
            service.handle(self.request("artifact", run_id=7, name="other"))
        service.github.download_artifact.assert_not_called()

    def test_broker_config_rejects_symlink_even_when_target_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "token"
            token.write_text("secret-token\n")
            token.chmod(0o600)
            real = root / "config.json"
            real.write_text(json.dumps({
                "github_repository": "owner/repo",
                "workflow": "orchestra-worker.yml",
                "workflow_ref": "main",
                "workflow_sha": TASK_SHA,
                "credential_file": str(token),
                "service_uid": os.geteuid(),
                "coordinator_uid": os.geteuid() + 1,
                "allowed_models": {"codex": ["gpt-5-codex"]},
            }))
            real.chmod(0o600)
            link = root / "linked-config.json"
            link.symlink_to(real)
            with self.assertRaisesRegex(PermissionError, "service-owner-only"):
                ServiceConfig.load(link)


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
                "ORCHESTRA_DISPATCH_ID": DISPATCH_ID,
                "ORCHESTRA_DISPATCHED_AT": DISPATCHED_AT,
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
        self.assertTrue(
            run.call_args_list[0].kwargs["env"]["HOME"].endswith("runtime/codex-home")
        )
        self.assertNotIn("DEEPSEEK_API_KEY", run.call_args_list[0].kwargs["env"])
        for call in run.call_args_list[1:]:
            self.assertNotIn("OPENAI_API_KEY", call.kwargs["env"])
            self.assertNotIn("DEEPSEEK_API_KEY", call.kwargs["env"])

    def test_workflow_actions_are_pinned_to_full_commit_shas(self):
        workflows = list(
            (Path(__file__).resolve().parents[1] / ".github/workflows").glob("*.yml")
        )
        text = "\n".join(path.read_text() for path in workflows)
        for line in text.splitlines():
            if "uses:" in line:
                self.assertRegex(line, r"uses: [^@]+@[0-9a-f]{40}(?:\s|$)")

    def test_arm64_verification_is_reproducible_and_manual_only(self):
        root = Path(__file__).resolve().parents[1]
        worker = (root / ".github/workflows/orchestra-worker.yml").read_text()
        verification = (
            root / ".github/workflows/orchestra-security-ci.yml"
        ).read_text()
        self.assertIn("runs-on: ubuntu-24.04-arm", worker)
        self.assertIn("runs-on: ubuntu-24.04-arm", verification)
        self.assertIn("workflow_dispatch:", verification)
        self.assertNotIn("pull_request:", verification)
        self.assertNotIn("push:", verification)

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

    def test_workflow_verifies_sha_before_environment_can_release_secrets(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/orchestra-worker.yml"
        ).read_text()
        verification, execution = workflow.split("  execute:\n", 1)
        self.assertIn("workflow_sha:", workflow)
        self.assertIn("verify-workflow-sha:", verification)
        self.assertIn("ACTUAL_WORKFLOW_SHA: ${{ github.sha }}", verification)
        self.assertNotIn("environment: orchestra-workers", verification)
        self.assertNotIn("OPENAI_API_KEY", verification)
        self.assertNotIn("DEEPSEEK_API_KEY", verification)
        self.assertIn("needs: verify-workflow-sha", execution)
        self.assertIn("environment: orchestra-workers", execution)
        self.assertIn("ref: ${{ inputs.workflow_sha }}", execution)


if __name__ == "__main__":
    unittest.main()
