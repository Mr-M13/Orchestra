"""Bounded GitHub Actions dispatch for ephemeral Orchestra workers.

The coordinator remains authoritative.  GitHub Actions only executes one
already-approved task in a disposable checkout and returns a patch plus a
bounded result artifact.  No task can choose a workflow, repository, ref,
credential, command or container; those are owner configuration.

This module uses the GitHub REST API directly so PixelBox does not need a
privileged ``gh`` installation.  The owner-only token is sent only in an HTTP
Authorization header and is never placed in a command line, task record, log,
audit event or error message.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from omcp.platform import ensure_owner_only


DEFAULT_API_URL = "https://api.github.com"
MAX_HTTP_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_INSTRUCTION_BYTES = 65_536
ALLOWED_ARTIFACT_FILES = {"result.txt", "patch.diff", "worker.log", "summary.json"}
RUN_NAME_PREFIX = "Orchestra "


class GitHubActionsError(RuntimeError):
    """A sanitized GitHub Actions failure."""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow HTTPS redirects without forwarding credentials cross-origin."""

    def redirect_request(self, request: urllib.request.Request, *args: Any,
                         **kwargs: Any) -> urllib.request.Request | None:
        redirected = super().redirect_request(request, *args, **kwargs)
        if redirected is None:
            return None
        old = urllib.parse.urlsplit(request.full_url)
        new = urllib.parse.urlsplit(redirected.full_url)
        if new.scheme != "https":
            raise urllib.error.HTTPError(
                redirected.full_url, 403, "unsafe redirect", {}, None,
            )
        old_origin = (old.scheme.lower(), old.hostname, old.port)
        new_origin = (new.scheme.lower(), new.hostname, new.port)
        if old_origin != new_origin:
            redirected.remove_header("Authorization")
        return redirected


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(fd)
    temporary.replace(path)
    os.chmod(path, 0o600)


def encode_instructions(instructions: str, maximum: int = 48_000) -> str:
    """Encode task input for a workflow_dispatch input field.

    The dispatch API accepts a bounded JSON body.  zlib keeps ordinary tasks
    well below that limit; an oversized task fails before any remote side
    effect instead of being silently truncated.
    """
    if not isinstance(instructions, str) or not instructions:
        raise ValueError("instructions must be non-empty")
    import zlib

    raw = instructions.encode("utf-8")
    if len(raw) > MAX_INSTRUCTION_BYTES:
        raise ValueError("task instructions are too large for a GitHub Actions dispatch")
    encoded = base64.urlsafe_b64encode(
        zlib.compress(raw, 9)
    ).decode("ascii")
    if len(encoded) > maximum:
        raise ValueError(
            "task instructions are too large for a GitHub Actions dispatch"
        )
    return encoded


def decode_instructions(encoded: str) -> str:
    import zlib

    if not isinstance(encoded, str) or not encoded:
        raise ValueError("encoded instructions are empty")
    try:
        compressed = base64.b64decode(
            encoded.encode("ascii"), altchars=b"-_", validate=True,
        )
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(compressed, MAX_INSTRUCTION_BYTES + 1)
        if len(raw) > MAX_INSTRUCTION_BYTES or decompressor.unconsumed_tail:
            raise ValueError("encoded instructions exceed the configured bound")
        raw += decompressor.flush(MAX_INSTRUCTION_BYTES + 1 - len(raw))
        if len(raw) > MAX_INSTRUCTION_BYTES:
            raise ValueError("encoded instructions exceed the configured bound")
        if not decompressor.eof or decompressor.unused_data:
            raise ValueError("encoded instructions are invalid")
    except (UnicodeEncodeError, binascii.Error, zlib.error) as exc:
        raise ValueError("encoded instructions are invalid") from exc
    return raw.decode("utf-8")


def _validate_repository(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value,
    ):
        raise ValueError("GitHub repository must be owner/name")
    return value


def _validate_workflow(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+\.ya?ml", value,
    ):
        raise ValueError("GitHub workflow must be a workflow file name")
    return value


@dataclass(frozen=True)
class RunState:
    run_id: int
    status: str
    conclusion: str | None
    url: str | None


class GitHubActionsClient:
    """Small, bounded REST client with no shell or token-in-argument path."""

    def __init__(
        self,
        repository: str,
        workflow: str,
        ref: str,
        credential_file: Path,
        api_url: str = DEFAULT_API_URL,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.repository = _validate_repository(repository)
        self.workflow = _validate_workflow(workflow)
        if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", ref):
            raise ValueError("GitHub workflow ref is invalid")
        if ".." in ref.split("/") or ref.startswith("-"):
            raise ValueError("GitHub workflow ref is unsafe")
        self.ref = ref
        self.api_url = api_url.rstrip("/")
        parsed = urllib.parse.urlparse(self.api_url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (parsed.scheme not in ({"http", "https"} if loopback else {"https"})
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("GitHub API URL must use HTTPS")
        self.credential_file = Path(credential_file)
        ensure_owner_only(self.credential_file)
        self._opener = opener or urllib.request.build_opener(
            _SafeRedirectHandler(),
        ).open
        self.sleep = sleep
        self._token: str | None = None

    def _credential(self) -> str:
        if self._token is None:
            try:
                value = self.credential_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise GitHubActionsError("GitHub token file is unavailable") from exc
            if not value or len(value) > 8192 or any(character.isspace() for character in value):
                raise GitHubActionsError("GitHub token file is invalid")
            self._token = value
        return self._token

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
        *, accept: str = "application/vnd.github+json", maximum: int = MAX_HTTP_BYTES,
        raw: bool = False,
    ) -> Any:
        url = self.api_url + path
        data = None
        headers = {
            "Authorization": "Bearer " + self._credential(),
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "orchestra-omcp",
        }
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener(request, timeout=30) as response:
                body = response.read(maximum + 1)
                if len(body) > maximum:
                    raise GitHubActionsError("GitHub response exceeded the configured bound")
                if raw:
                    return body
                if not body:
                    return {}
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            # GitHub error bodies can echo request metadata.  Keep only the
            # numeric status so task input and headers never reach logs.
            raise GitHubActionsError(
                f"GitHub API request failed with HTTP {exc.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise GitHubActionsError(
                f"GitHub API request failed ({type(exc).__name__})"
            ) from None

    @property
    def _workflow_path(self) -> str:
        workflow = urllib.parse.quote(self.workflow, safe="")
        return f"/repos/{self.repository}/actions/workflows/{workflow}"

    def dispatch(self, task_id: str, request_id: str, agent: str, model: str,
                 instructions: str) -> str:
        """Dispatch one workflow run and return the encoded dispatch marker."""
        if not isinstance(task_id, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            task_id,
        ):
            raise ValueError("invalid task_id for GitHub dispatch")
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", request_id,
        ):
            raise ValueError("invalid request_id for GitHub dispatch")
        if agent not in {"codex", "deepseek"}:
            raise ValueError("unsupported GitHub Actions agent")
        if not isinstance(model, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:/-]{1,128}", model,
        ):
            raise ValueError("unsupported GitHub Actions model")
        encoded = encode_instructions(instructions)
        self._request("POST", self._workflow_path + "/dispatches", {
            "ref": self.ref,
            "inputs": {
                "task_id": task_id,
                "request_id": request_id,
                "agent": agent,
                "model": model,
                "payload": encoded,
            },
        })
        return encoded

    def find_run(self, task_id: str) -> RunState | None:
        """Find a run by the workflow's deterministic run-name."""
        query = urllib.parse.urlencode({
            "event": "workflow_dispatch", "per_page": 100,
        })
        result = self._request(
            "GET", self._workflow_path + "/runs?" + query,
        )
        runs = result.get("workflow_runs") if isinstance(result, dict) else None
        if not isinstance(runs, list):
            return None
        expected = RUN_NAME_PREFIX + task_id
        for run in runs:
            if not isinstance(run, dict):
                continue
            if run.get("display_title") != expected:
                continue
            run_id = run.get("id")
            if not isinstance(run_id, int):
                continue
            return RunState(
                run_id, str(run.get("status", "unknown")),
                run.get("conclusion"), run.get("html_url"),
            )
        return None

    def wait_for_run(self, task_id: str, seconds: int = 90) -> RunState:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            found = self.find_run(task_id)
            if found is not None:
                return found
            self.sleep(3)
        raise GitHubActionsError("GitHub Actions run was not created")

    def status(self, run_id: int) -> RunState:
        result = self._request(
            "GET", f"/repos/{self.repository}/actions/runs/{int(run_id)}",
        )
        if not isinstance(result, dict):
            raise GitHubActionsError("invalid GitHub Actions run response")
        return RunState(
            int(run_id), str(result.get("status", "unknown")),
            result.get("conclusion"), result.get("html_url"),
        )

    def cancel(self, run_id: int) -> None:
        self._request(
            "POST", f"/repos/{self.repository}/actions/runs/{int(run_id)}/cancel",
        )

    def list_artifacts(self, run_id: int) -> list[dict[str, Any]]:
        result = self._request(
            "GET", f"/repos/{self.repository}/actions/runs/{int(run_id)}/artifacts",
        )
        artifacts = result.get("artifacts") if isinstance(result, dict) else None
        return [item for item in artifacts or [] if isinstance(item, dict)]

    def download_artifact(self, run_id: int, name: str) -> dict[str, bytes]:
        """Download and validate one named artifact into memory."""
        artifact = next(
            (item for item in self.list_artifacts(run_id)
             if item.get("name") == name and item.get("expired") is not True),
            None,
        )
        if artifact is None:
            raise GitHubActionsError("GitHub Actions artifact is unavailable")
        archive_url = artifact.get("archive_download_url")
        if not isinstance(archive_url, str) or not archive_url.startswith(self.api_url + "/"):
            raise GitHubActionsError("GitHub Actions artifact URL is invalid")
        body = self._request(
            "GET", archive_url[len(self.api_url):],
            accept="application/vnd.github+json",
            maximum=MAX_ARTIFACT_BYTES, raw=True,
        )
        try:
            archive = zipfile.ZipFile(io.BytesIO(body))
        except zipfile.BadZipFile as exc:
            raise GitHubActionsError("GitHub Actions artifact is not a valid zip") from exc
        files: dict[str, bytes] = {}
        with archive:
            total_size = 0
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name_only = Path(info.filename).name
                if name_only != info.filename or name_only not in ALLOWED_ARTIFACT_FILES:
                    raise GitHubActionsError("GitHub Actions artifact contains an unsafe path")
                if name_only in files:
                    raise GitHubActionsError("GitHub Actions artifact contains duplicate files")
                if info.flag_bits & 0x1:
                    raise GitHubActionsError("GitHub Actions artifact contains encrypted files")
                if info.file_size > MAX_ARTIFACT_BYTES:
                    raise GitHubActionsError("GitHub Actions artifact file is too large")
                total_size += info.file_size
                if total_size > MAX_ARTIFACT_BYTES:
                    raise GitHubActionsError("GitHub Actions artifact is too large")
                data = archive.read(info)
                if len(data) != info.file_size:
                    raise GitHubActionsError("GitHub Actions artifact file size is invalid")
                files[name_only] = data
        if "result.txt" not in files and "patch.diff" not in files:
            raise GitHubActionsError("GitHub Actions artifact has no result")
        return files


def load_spec(task_dir: Path) -> dict[str, Any]:
    path = Path(task_dir) / "github-actions.json"
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("invalid GitHub Actions spec")
    return value


def client_from_spec(spec: dict[str, Any]) -> GitHubActionsClient:
    return GitHubActionsClient(
        str(spec["github_repository"]), str(spec["workflow"]),
        str(spec["workflow_ref"]), Path(str(spec["credential_file"])),
        api_url=str(spec.get("github_api_url") or DEFAULT_API_URL),
    )


def cancel_task_run(task_dir: Path, reason: str = "task cancelled") -> dict[str, Any] | None:
    """Best-effort cancellation using persisted run identity only."""
    task_dir = Path(task_dir)
    try:
        state = json.loads((task_dir / "github-state.json").read_text())
        spec = load_spec(task_dir)
    except (FileNotFoundError, ValueError, KeyError):
        return None
    run_id = state.get("run_id") if isinstance(state, dict) else None
    client = client_from_spec(spec)
    if not isinstance(run_id, int):
        # The runner may have dispatched and died before persisting the run ID.
        # Adopt the deterministic run name and cancel that exact run.
        found = client.find_run(str(spec.get("task_id", "")))
        if found is None:
            return None
        run_id = found.run_id
    client.cancel(run_id)
    return {"run_id": run_id, "reason": reason[:500]}


def write_state(path: Path, run: RunState, **extra: Any) -> None:
    value = {
        "schema_version": 1,
        "run_id": run.run_id,
        "status": run.status,
        "conclusion": run.conclusion,
        "url": run.url,
        "updated_at": utc_now(),
        **extra,
    }
    _atomic_json(path, value)
