"""Detached runner for one GitHub Actions worker task.

The coordinator starts this process in a new session, exactly like local worker
runners.  It publishes a startup lease, dispatches or adopts one deterministic
workflow run, persists remote identity before polling, downloads a bounded
artifact and records a completion file even on cancellation or failure.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from omcp.github_actions import (
    DispatchIdentity, GitHubActionsError, RunState,
    load_or_create_dispatch_identity, write_state,
)
from omcp.github_dispatch_service import DispatchClient
from omcp.runner import (
    RunnerTerminated, atomic_json, utc_now, write_private_text,
    write_startup_lease,
)


def _spec_paths(spec: dict[str, Any]) -> tuple[Path, Path, Path, Path, Path]:
    return tuple(  # type: ignore[return-value]
        Path(spec[key]) for key in (
            "prompt_path", "log_path", "completion_path", "result_path", "workspace",
        )
    )


def _bounded_text(value: str, maximum: int) -> str:
    return value.encode("utf-8")[:maximum].decode("utf-8", "ignore")


def _apply_patch(workspace: Path, task_dir: Path, patch: bytes, maximum: int) -> None:
    if not patch.strip():
        return
    if len(patch) > maximum:
        raise RuntimeError("remote patch exceeded the configured result bound")
    patch_path = task_dir / "remote.patch.diff"
    write_private_text(patch_path, patch.decode("utf-8", "replace"))
    git = "/usr/bin/git"
    git_home = task_dir / "git-home"
    git_home.mkdir(mode=0o700, exist_ok=True)
    git_environment = {
        key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL")
        if key in os.environ
    }
    git_environment.update({
        "HOME": str(git_home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    check = subprocess.run(
        [git, "-C", str(workspace), "apply", "--check", str(patch_path)],
        capture_output=True, text=True, check=False, timeout=60,
        env=git_environment,
    )
    if check.returncode:
        raise RuntimeError("remote patch does not apply to the base worktree")
    applied = subprocess.run(
        [git, "-C", str(workspace), "apply", str(patch_path)],
        capture_output=True, text=True, check=False, timeout=60,
        env=git_environment,
    )
    if applied.returncode:
        raise RuntimeError("remote patch application failed")


def _cancelled(task_dir: Path) -> bool:
    path = task_dir / "cancel.json"
    return path.exists()


def _write_remote_artifact(task_dir: Path, files: dict[str, bytes]) -> Path:
    artifact_dir = task_dir / "github-artifact"
    artifact_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(artifact_dir, 0o700)
    for name, data in files.items():
        path = artifact_dir / name
        write_private_text(path, data.decode("utf-8", "replace"))
    return artifact_dir


def run(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text())
    write_startup_lease(spec_path, spec)
    task_dir = spec_path.parent
    prompt, log, completion, result, workspace = _spec_paths(spec)
    task_id = str(spec["task_id"])
    maximum = int(spec.get("max_response_bytes", 65_536))
    deadline_seconds = int(spec.get("max_worker_seconds", 1_800))
    poll_seconds = int(spec.get("poll_seconds", 10))
    state_path = Path(spec.get("remote_state_path") or (task_dir / "github-state.json"))
    if state_path.resolve(strict=False).parent != task_dir.resolve():
        raise ValueError("remote state must stay in the private task directory")
    artifact_name = str(spec.get("artifact_name") or ("omcp-" + task_id))
    client: DispatchClient | None = None
    run_state: RunState | None = None
    identity: DispatchIdentity | None = None
    exit_code, error_type = 125, None
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def terminate(signum: int, _frame: Any) -> None:
        raise RunnerTerminated(signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        client = DispatchClient(
            Path(str(spec["github_dispatch_socket"])),
            int(spec["github_service_uid"]),
        )
        request_id = str(spec.get("request_id", task_id))
        identity, resumed = load_or_create_dispatch_identity(
            state_path, task_id, request_id,
        )
        # A restart may adopt only the exact pre-persisted nonce, dispatch time,
        # configured ref and head SHA.  If it is not visible, fail closed rather
        # than dispatching a possible duplicate.
        run_state = client.find_run(identity)
        if _cancelled(task_dir):
            # A queued runner can start after the coordinator has already
            # recorded cancellation.  Adopt and cancel an existing run, but do
            # not create a new remote side effect for a cancelled task.
            exit_code, error_type = 130, "Cancelled"
            _cancel_quietly(client, run_state, state_path, identity)
            return exit_code
        if run_state is None:
            if resumed:
                raise GitHubActionsError(
                    "prepared GitHub dispatch has no exact adoptable run"
                )
            # Close the cancellation race as near as possible to dispatch.
            if _cancelled(task_dir):
                exit_code, error_type = 130, "Cancelled"
                return exit_code
            client.dispatch(
                identity, str(spec["agent"]), str(spec["model"]),
                prompt.read_text(encoding="utf-8"),
            )
            run_state = client.wait_for_run(identity)
        write_state(state_path, run_state, identity)
        started = time.monotonic()
        while True:
            if _cancelled(task_dir):
                client.cancel(run_state.run_id, identity)
                write_state(
                    state_path, run_state, identity,
                    cancellation_requested_at=utc_now(),
                )
                exit_code, error_type = 130, "Cancelled"
                break
            if time.monotonic() - started > deadline_seconds:
                client.cancel(run_state.run_id, identity)
                exit_code, error_type = 124, "WorkerTimeout"
                break
            run_state = client.status(run_state.run_id, identity)
            write_state(state_path, run_state, identity)
            if run_state.status == "completed":
                if run_state.conclusion != "success":
                    exit_code = 1
                    error_type = "GitHubActionsConclusion"
                    break
                files = None
                for attempt in range(12):
                    try:
                        files = client.download_artifact(
                            run_state.run_id, artifact_name, identity,
                        )
                        break
                    except GitHubActionsError:
                        # Artifact registration can lag the completed run by a
                        # few seconds.  Retry the same run identity; never
                        # dispatch a replacement.
                        if attempt == 11:
                            raise
                        client.sleep(5)
                assert files is not None
                _write_remote_artifact(task_dir, files)
                patch = files.get("patch.diff", b"")
                _apply_patch(workspace, task_dir, patch, maximum)
                text = files.get("result.txt", b"").decode("utf-8", "replace")
                if not text:
                    text = "GitHub Actions worker completed; no textual result was returned.\n"
                write_private_text(result, _bounded_text(text, maximum))
                if "worker.log" in files:
                    write_private_text(
                        log, files["worker.log"].decode("utf-8", "replace")[:maximum],
                    )
                exit_code = 0
                break
            time.sleep(max(2, min(poll_seconds, 30)))
    except RunnerTerminated as exc:
        error_type = type(exc).__name__
        exit_code = 128 + exc.signum
        _cancel_quietly(client, run_state, state_path, identity)
    except (GitHubActionsError, OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        error_type = type(exc).__name__
        exit_code = 1
        _cancel_quietly(client, run_state, state_path, identity)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        if exit_code != 0:
            message = (
                "GitHub Actions task was cancelled.\n" if exit_code in {130, 128 + signal.SIGTERM}
                else "GitHub Actions task failed; no remote diagnostics were retained.\n"
            )
            try:
                write_private_text(result, _bounded_text(message, maximum))
            except OSError:
                pass
        atomic_json(completion, {
            "exit_code": exit_code, "finished_at": utc_now(),
            "runner_error": error_type,
        })
    return exit_code


def _cancel_quietly(client: DispatchClient | None,
                    run_state: RunState | None, state_path: Path,
                    identity: DispatchIdentity | None = None) -> None:
    if client is None or identity is None:
        return
    try:
        if run_state is None:
            # SIGTERM can arrive after dispatch but before the run ID was
            # persisted.  Adopt the deterministic run and cancel it rather than
            # leaving an orphan executing in GitHub.
            run_state = client.find_run(identity)
        if run_state is None:
            return
        client.cancel(run_state.run_id, identity)
        write_state(
            state_path, run_state, identity,
            cancellation_requested_at=utc_now(),
        )
    except Exception:
        # The completion record remains authoritative; an operator can cancel
        # the exact run URL from the persisted safe metadata.
        return


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run one GitHub Actions worker task")
    parser.add_argument("--spec", type=Path, required=True)
    return run(parser.parse_args(argv).spec)


if __name__ == "__main__":
    raise SystemExit(main())
