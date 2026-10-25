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
import hashlib
import io
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


DEFAULT_API_URL = "https://api.github.com"
MAX_HTTP_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_INSTRUCTION_BYTES = 65_536
ALLOWED_ARTIFACT_FILES = {"result.txt", "patch.diff", "worker.log", "summary.json"}
RUN_NAME_PREFIX = "Orchestra "
WORKER_ENVIRONMENT = "orchestra-workers"
RUN_CREATION_SKEW = timedelta(seconds=30)
RUN_CREATION_WINDOW = timedelta(minutes=15)
GITHUB_API_VERSION = "2026-03-10"


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


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("invalid UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("invalid UTC timestamp")
    return parsed.astimezone(timezone.utc)


def ensure_service_credential(path: Path, expected_uid: int) -> None:
    """Require a token owned by this dedicated Unix service identity.

    The immediate credential directory is private as well as the file.  A
    coordinator or tunnel account therefore cannot reuse a token by pointing
    this process at a shared or symlinked path.  Cross-identity transfer must
    happen through the task/state protocol, never by sharing credential files.
    """
    if (not isinstance(expected_uid, int) or isinstance(expected_uid, bool)
            or expected_uid < 0 or os.geteuid() != expected_uid):
        raise PermissionError("GitHub adapter service identity does not match configuration")
    path = Path(path)
    try:
        file_stat = path.lstat()
        directory_stat = path.parent.lstat()
    except OSError as exc:
        raise PermissionError("GitHub token file is unavailable") from exc
    if (stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != expected_uid or file_stat.st_mode & 0o077):
        raise PermissionError("GitHub token must be an owner-only regular file")
    if (stat.S_ISLNK(directory_stat.st_mode)
            or not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != expected_uid
            or directory_stat.st_mode & 0o077):
        raise PermissionError("GitHub token directory must be owner-only")


def _read_service_credential(path: Path, expected_uid: int) -> str:
    """Open without following symlinks and revalidate the opened inode."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise GitHubActionsError("GitHub token file is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != expected_uid
                or opened.st_mode & 0o077):
            raise GitHubActionsError("GitHub token file is invalid")
        chunks = bytearray()
        while len(chunks) <= 8192:
            chunk = os.read(descriptor, 8193 - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
    finally:
        os.close(descriptor)
    try:
        value = bytes(chunks).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise GitHubActionsError("GitHub token file is invalid") from exc
    if not value or len(chunks) > 8192 or any(character.isspace() for character in value):
        raise GitHubActionsError("GitHub token file is invalid")
    return value


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


@dataclass(frozen=True)
class DispatchIdentity:
    task_id: str
    request_id: str
    dispatch_id: str
    dispatched_at: str
    source_sha: str
    agent: str
    model: str
    payload_sha256: str

    @property
    def run_name(self) -> str:
        return (
            RUN_NAME_PREFIX + self.task_id + " " + self.dispatch_id
            + " " + self.binding_sha256
        )

    @property
    def binding_sha256(self) -> str:
        fields = (
            self.task_id, self.request_id, self.dispatch_id, self.dispatched_at,
            self.source_sha, self.agent, self.model, self.payload_sha256,
        )
        return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LegacyDispatchIdentity:
    """Exact cancellation identity persisted by schema v2."""

    task_id: str
    request_id: str
    dispatch_id: str
    dispatched_at: str

    @property
    def run_name(self) -> str:
        return RUN_NAME_PREFIX + self.task_id + " " + self.dispatch_id


def _validate_identity_fields(
    task_id: str, request_id: str, dispatch_id: str, dispatched_at: str,
) -> None:
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        task_id,
    ):
        raise ValueError("invalid task_id for GitHub dispatch")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
        raise ValueError("invalid request_id for GitHub dispatch")
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        dispatch_id,
    ):
        raise ValueError("invalid GitHub dispatch identity")
    _parse_utc(dispatched_at)


def _validate_legacy_dispatch_identity(
    identity: LegacyDispatchIdentity,
) -> LegacyDispatchIdentity:
    _validate_identity_fields(
        identity.task_id, identity.request_id, identity.dispatch_id,
        identity.dispatched_at,
    )
    return identity


def _validate_dispatch_identity(identity: DispatchIdentity) -> DispatchIdentity:
    _validate_identity_fields(
        identity.task_id, identity.request_id, identity.dispatch_id,
        identity.dispatched_at,
    )
    if not re.fullmatch(r"[0-9a-f]{40}", identity.source_sha):
        raise ValueError("invalid GitHub source SHA")
    if identity.agent not in {"codex", "deepseek"}:
        raise ValueError("unsupported GitHub Actions agent")
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", identity.model):
        raise ValueError("unsupported GitHub Actions model")
    if not re.fullmatch(r"[0-9a-f]{64}", identity.payload_sha256):
        raise ValueError("invalid GitHub Actions payload digest")
    return identity


def load_or_create_dispatch_identity(
    path: Path, task_id: str, request_id: str, source_sha: str,
    agent: str, model: str, payload_sha256: str,
) -> tuple[DispatchIdentity, bool]:
    """Persist identity and dispatch time before any remote side effect."""
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid GitHub dispatch state")
        if value.get("schema_version") != 4 or value.get("phase") not in {
            "prepared", "adopted",
        }:
            raise ValueError("unsupported GitHub dispatch state")
        identity = DispatchIdentity(
            str(value.get("task_id", "")), str(value.get("request_id", "")),
            str(value.get("dispatch_id", "")), str(value.get("dispatched_at", "")),
            str(value.get("source_sha", "")),
            str(value.get("agent", "")), str(value.get("model", "")),
            str(value.get("payload_sha256", "")),
        )
        _validate_dispatch_identity(identity)
        if (identity.task_id != task_id or identity.request_id != request_id
                or identity.source_sha != source_sha or identity.agent != agent
                or identity.model != model
                or identity.payload_sha256 != payload_sha256):
            raise ValueError("GitHub dispatch state does not match this task")
        return identity, True
    identity = _validate_dispatch_identity(DispatchIdentity(
        task_id, request_id, str(uuid4()), utc_now(), source_sha,
        agent, model, payload_sha256,
    ))
    _atomic_json(path, {
        "schema_version": 4, "phase": "prepared",
        "task_id": identity.task_id, "request_id": identity.request_id,
        "dispatch_id": identity.dispatch_id, "dispatched_at": identity.dispatched_at,
        "source_sha": identity.source_sha,
        "agent": identity.agent, "model": identity.model,
        "payload_sha256": identity.payload_sha256,
    })
    return identity, False


class GitHubActionsClient:
    """Small, bounded REST client with no shell or token-in-argument path."""

    def __init__(
        self,
        repository: str,
        workflow: str,
        ref: str,
        head_sha: str,
        credential_file: Path,
        service_uid: int | None = None,
        dispatch_actor: str | None = None,
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
        if ref.startswith("refs/heads/"):
            ref = ref.removeprefix("refs/heads/")
        elif ref.startswith("refs/"):
            raise ValueError("GitHub workflow ref must name a branch")
        if not ref or re.fullmatch(r"[0-9a-fA-F]{40}", ref):
            raise ValueError("GitHub workflow ref must name a branch")
        self.ref = ref
        if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
            raise ValueError("GitHub workflow head SHA must be a full commit SHA")
        self.head_sha = head_sha.lower()
        self.api_url = api_url.rstrip("/")
        parsed = urllib.parse.urlparse(self.api_url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (parsed.scheme not in ({"http", "https"} if loopback else {"https"})
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("GitHub API URL must use HTTPS")
        self.credential_file = Path(credential_file)
        self.service_uid = os.geteuid() if service_uid is None else service_uid
        if dispatch_actor is not None and not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?", dispatch_actor,
        ):
            raise ValueError("GitHub dispatch actor is invalid")
        self.dispatch_actor = dispatch_actor
        ensure_service_credential(self.credential_file, self.service_uid)
        self._opener = opener or urllib.request.build_opener(
            _SafeRedirectHandler(),
        ).open
        self.sleep = sleep
        self._token: str | None = None

    def _credential(self) -> str:
        if self._token is None:
            self._token = _read_service_credential(
                self.credential_file, self.service_uid,
            )
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
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
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

    def dispatch(self, identity: DispatchIdentity, agent: str, model: str,
                 instructions: str) -> RunState:
        """Verify the protected ref, then dispatch one SHA-bound workflow run.

        The workflow performs the same SHA comparison in a credential-free job
        before its environment-gated execution job can receive provider keys.
        This second comparison closes the ref-move window between this lookup
        and GitHub accepting the dispatch.
        """
        _validate_dispatch_identity(identity)
        if agent != identity.agent or model != identity.model:
            raise ValueError("GitHub dispatch parameters do not match identity")
        encoded = encode_instructions(instructions)
        if hashlib.sha256(encoded.encode("ascii")).hexdigest() != identity.payload_sha256:
            raise ValueError("GitHub dispatch payload does not match identity")
        self._verify_protected_ref(identity.source_sha)
        self._verify_secret_environment()
        _ = self._request("POST", self._workflow_path + "/dispatches", {
            "ref": self.ref,
            "return_run_details": True,
            "inputs": {
                "task_id": identity.task_id,
                "request_id": identity.request_id,
                "dispatch_id": identity.dispatch_id,
                "dispatched_at": identity.dispatched_at,
                "agent": agent,
                "model": model,
                "workflow_sha": self.head_sha,
                "source_sha": identity.source_sha,
                "dispatch_binding": identity.binding_sha256,
                "payload_sha256": identity.payload_sha256,
                "payload": encoded,
            },
        })
        query = urllib.parse.urlencode({
            "event": "workflow_dispatch", "branch": self.ref, "per_page": 100,
        })
        for attempt in range(6):
            discovered = self._request("GET", self._workflow_path + "/runs?" + query)
            runs = discovered.get("workflow_runs") if isinstance(discovered, dict) else None
            matches = [
                state for item in runs or []
                if (state := self._run_state_for_name(item, identity.run_name, identity.dispatched_at, require_current_head=True, require_first_attempt=True)) is not None
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise GitHubActionsError("GitHub dispatch matched multiple trusted runs")
            if attempt < 5:
                self.sleep(1)
        raise GitHubActionsError("GitHub dispatch did not expose an exact trusted run")

    def _verify_protected_ref(self, source_sha: str) -> None:
        """Fail closed unless the configured branch is protected at its exact SHA."""
        branch = urllib.parse.quote(self.ref, safe="")
        result = self._request(
            "GET", f"/repos/{self.repository}/branches/{branch}",
        )
        commit = result.get("commit") if isinstance(result, dict) else None
        resolved_sha = commit.get("sha") if isinstance(commit, dict) else None
        protected = result.get("protected") if isinstance(result, dict) else None
        if protected is not True:
            raise GitHubActionsError("GitHub workflow ref is not a protected branch")
        if (not isinstance(resolved_sha, str)
                or resolved_sha.lower() != self.head_sha):
            raise GitHubActionsError("GitHub workflow ref does not match configured SHA")
        if resolved_sha.lower() != source_sha:
            raise GitHubActionsError("GitHub source SHA does not match protected ref")

    def _verify_secret_environment(self) -> None:
        """Require a protected-branch environment with no human review gate."""
        environment = urllib.parse.quote(WORKER_ENVIRONMENT, safe="")
        result = self._request(
            "GET", f"/repos/{self.repository}/environments/{environment}",
        )
        rules = result.get("protection_rules") if isinstance(result, dict) else None
        policy = (
            result.get("deployment_branch_policy")
            if isinstance(result, dict) else None
        )
        if (not isinstance(rules, list)
                or any(
                    isinstance(rule, dict)
                    and rule.get("type") == "required_reviewers"
                    for rule in rules
                )
                or not isinstance(policy, dict)
                or policy.get("protected_branches") is not True):
            raise GitHubActionsError(
                "GitHub worker environment must use protected branches without human review"
            )

    def _run_state_for_name(
        self, run: object, run_name: str, dispatched_at: str,
        *, require_current_head: bool, require_first_attempt: bool,
        require_creation_window: bool = True,
    ) -> RunState | None:
        if not isinstance(run, dict):
            return None
        if (run.get("display_title") != run_name
                or run.get("event") != "workflow_dispatch"):
            return None
        head_sha = str(run.get("head_sha", "")).lower()
        if (not re.fullmatch(r"[0-9a-f]{40}", head_sha)
                or require_current_head and head_sha != self.head_sha
                or require_first_attempt and run.get("run_attempt") != 1):
            return None
        ref_name = self.ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
        if run.get("head_branch") != ref_name:
            return None
        workflow_path = str(run.get("path", ""))
        expected_path = f".github/workflows/{self.workflow}"
        if workflow_path not in {
            expected_path,
            f"{expected_path}@{self.ref}",
            f"{expected_path}@refs/heads/{self.ref}",
        }:
            return None
        actor = run.get("actor")
        triggering_actor = run.get("triggering_actor")
        if (self.dispatch_actor is None
                or not isinstance(actor, dict) or not isinstance(triggering_actor, dict)
                or str(actor.get("login", "")).lower() != self.dispatch_actor.lower()
                or str(triggering_actor.get("login", "")).lower()
                != self.dispatch_actor.lower()):
            return None
        try:
            created = _parse_utc(str(run.get("created_at", "")))
        except ValueError:
            return None
        dispatched = _parse_utc(dispatched_at)
        if (require_creation_window
                and not dispatched - RUN_CREATION_SKEW <= created <= dispatched + RUN_CREATION_WINDOW):
            return None
        run_id = run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            return None
        conclusion = run.get("conclusion")
        url = run.get("html_url")
        return RunState(
            run_id, str(run.get("status", "unknown")),
            conclusion if isinstance(conclusion, str) or conclusion is None else None,
            url if isinstance(url, str) or url is None else None,
        )

    def _run_state(self, run: object, identity: DispatchIdentity) -> RunState | None:
        return self._run_state_for_name(
            run, identity.run_name, identity.dispatched_at,
            require_current_head=True, require_first_attempt=True,
        )

    def _legacy_run_state(
        self, run: object, identity: LegacyDispatchIdentity,
    ) -> RunState | None:
        # Legacy state predates the source/payload binding, so this verifier is
        # available only to the cancellation drain. The exact random dispatch
        # identity, trusted actor, configured workflow/ref and creation window
        # still have to match; a changed workflow SHA is expected during rollout.
        return self._run_state_for_name(
            run, identity.run_name, identity.dispatched_at,
            require_current_head=False, require_first_attempt=False,
        )

    def status(self, run_id: int, identity: DispatchIdentity) -> RunState:
        result = self._request(
            "GET", f"/repos/{self.repository}/actions/runs/{int(run_id)}",
        )
        state = self._run_state_for_name(
            result, identity.run_name, identity.dispatched_at,
            require_current_head=True, require_first_attempt=True,
            require_creation_window=False,
        )
        if state is None or state.run_id != int(run_id):
            raise GitHubActionsError("invalid GitHub Actions run response")
        return state

    def cancel(self, run_id: int, identity: DispatchIdentity) -> None:
        self.status(run_id, identity)
        self._request(
            "POST", f"/repos/{self.repository}/actions/runs/{int(run_id)}/cancel",
        )

    def drain_legacy(
        self, identity: LegacyDispatchIdentity, run_id: int | None,
    ) -> RunState:
        """Cancel one exact schema-v2 run, or prove it is terminal."""
        _validate_legacy_dispatch_identity(identity)
        if run_id is None:
            query = urllib.parse.urlencode({
                "event": "workflow_dispatch", "branch": self.ref, "per_page": 100,
            })
            result = self._request(
                "GET", self._workflow_path + "/runs?" + query,
            )
            runs = result.get("workflow_runs") if isinstance(result, dict) else None
            state = next(
                (candidate for item in runs or []
                 if (candidate := self._legacy_run_state(item, identity)) is not None),
                None,
            )
            if state is None:
                raise GitHubActionsError("legacy GitHub Actions run is not drainable")
        else:
            if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
                raise ValueError("legacy GitHub Actions run ID is invalid")
            result = self._request(
                "GET", f"/repos/{self.repository}/actions/runs/{run_id}",
            )
            state = self._legacy_run_state(result, identity)
            if state is None or state.run_id != run_id:
                raise GitHubActionsError("invalid legacy GitHub Actions run response")
        if state.status != "completed":
            self._request(
                "POST", f"/repos/{self.repository}/actions/runs/{state.run_id}/cancel",
            )
        return state

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


def drain_legacy_task_run(
    task_dir: Path, reason: str = "schema-v2 rollout drain",
) -> dict[str, Any]:
    """Drain one schema-v2 dispatch and durably record the migration."""
    task_dir = Path(task_dir)
    state_path = task_dir / "github-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("schema_version") != 2:
        raise ValueError("GitHub dispatch state is not schema v2")
    identity = _validate_legacy_dispatch_identity(LegacyDispatchIdentity(
        str(state.get("task_id", "")), str(state.get("request_id", "")),
        str(state.get("dispatch_id", "")), str(state.get("dispatched_at", "")),
    ))
    if task_dir.name != identity.task_id:
        raise ValueError("legacy GitHub dispatch state does not match task directory")
    run_id = state.get("run_id")
    if (run_id is not None
            and (not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0)):
        raise ValueError("legacy GitHub dispatch run ID is invalid")

    marker_path = task_dir / "github-legacy-drain.json"
    if marker_path.exists():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        expected = {
            "schema_version": 1,
            "legacy_schema_version": 2,
            "task_id": identity.task_id,
            "request_id": identity.request_id,
            "dispatch_id": identity.dispatch_id,
            "dispatched_at": identity.dispatched_at,
            "persisted_run_id": run_id,
        }
        resolved_run_id = marker.get("resolved_run_id") if isinstance(marker, dict) else None
        if (not isinstance(marker, dict)
                or any(marker.get(key) != value for key, value in expected.items())
                or marker.get("outcome") not in {
                    "cancellation_requested", "already_completed",
                }
                or not isinstance(resolved_run_id, int)
                or isinstance(resolved_run_id, bool) or resolved_run_id <= 0
                or (run_id is not None and resolved_run_id != run_id)
                or not isinstance(marker.get("drained_at"), str)):
            raise ValueError("legacy GitHub drain marker is invalid")
        _parse_utc(marker["drained_at"])
        return {
            "run_id": resolved_run_id,
            "reason": reason[:500],
            "outcome": marker["outcome"],
        }

    spec = load_spec(task_dir)
    if (str(spec.get("task_id", "")) != identity.task_id
            or str(spec.get("request_id", identity.task_id)) != identity.request_id):
        raise ValueError("legacy GitHub spec does not match dispatch state")
    from omcp.github_dispatch_service import DispatchClient
    client = DispatchClient(
        Path(str(spec["github_dispatch_socket"])), int(spec["github_service_uid"]),
    )
    remote = client.drain_legacy(identity, run_id)
    outcome = (
        "already_completed" if remote.status == "completed"
        else "cancellation_requested"
    )
    _atomic_json(marker_path, {
        "schema_version": 1,
        "legacy_schema_version": 2,
        "task_id": identity.task_id,
        "request_id": identity.request_id,
        "dispatch_id": identity.dispatch_id,
        "dispatched_at": identity.dispatched_at,
        "persisted_run_id": run_id,
        "resolved_run_id": remote.run_id,
        "remote_status": remote.status,
        "outcome": outcome,
        "drained_at": utc_now(),
    })
    return {"run_id": remote.run_id, "reason": reason[:500], "outcome": outcome}


def cancel_task_run(task_dir: Path, reason: str = "task cancelled") -> dict[str, Any] | None:
    """Best-effort cancellation using persisted run identity only."""
    task_dir = Path(task_dir)
    try:
        state = json.loads((task_dir / "github-state.json").read_text())
    except (FileNotFoundError, ValueError):
        return None
    if isinstance(state, dict) and state.get("schema_version") == 2:
        return drain_legacy_task_run(task_dir, reason)
    if not isinstance(state, dict) or state.get("schema_version") != 4:
        return None
    try:
        spec = load_spec(task_dir)
    except (FileNotFoundError, ValueError):
        return None
    run_id = state.get("run_id")
    if state.get("schema_version") != 4 or state.get("phase") != "adopted":
        return None
    try:
        identity = _validate_dispatch_identity(DispatchIdentity(
            str(state.get("task_id", "")), str(state.get("request_id", "")),
            str(state.get("dispatch_id", "")), str(state.get("dispatched_at", "")),
            str(state.get("source_sha", "")),
            str(state.get("agent", "")), str(state.get("model", "")),
            str(state.get("payload_sha256", "")),
        ))
    except ValueError:
        return None
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        return None
    from omcp.github_dispatch_service import DispatchClient
    client = DispatchClient(
        Path(str(spec["github_dispatch_socket"])), int(spec["github_service_uid"]),
    )
    client.cancel(run_id, identity)
    return {"run_id": run_id, "reason": reason[:500]}


def write_state(path: Path, run: RunState, identity: DispatchIdentity,
                **extra: Any) -> None:
    value = {
        "schema_version": 4,
        "phase": "adopted",
        "task_id": identity.task_id,
        "request_id": identity.request_id,
        "dispatch_id": identity.dispatch_id,
        "dispatched_at": identity.dispatched_at,
        "source_sha": identity.source_sha,
        "agent": identity.agent,
        "model": identity.model,
        "payload_sha256": identity.payload_sha256,
        "run_id": run.run_id,
        "status": run.status,
        "conclusion": run.conclusion,
        "url": run.url,
        "updated_at": utc_now(),
    }
    if set(extra) & set(value):
        raise ValueError("GitHub dispatch state metadata is immutable")
    value.update(extra)
    _atomic_json(path, value)
