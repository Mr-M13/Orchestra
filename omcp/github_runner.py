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
    GitHubActionsClient, GitHubActionsError, RunState, client_from_spec,
    write_state,
)
from omcp.platform import canonical_git_executable
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
    git = canonical_git_executable()
    check = subprocess.run(
        [git, "-C", str(workspace), "apply", "--check", str(patch_path)],
        capture_output=True, text=True, check=False, timeout=60,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check.returncode:
        raise RuntimeError("remote patch does not apply to the base worktree")
    applied = subprocess.run(
        [git, "-C", str(workspace), "apply", str(patch_path)],
        capture_output=True, text=True, check=False, timeout=60,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
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
    artifact_name = str(spec.get("artifact_name") or ("omcp-" + task_id))
    client: GitHubActionsClient | None = None
    run_state: RunState | None = None
    exit_code, error_type = 125, None
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def terminate(signum: int, _frame: Any) -> None:
        raise RunnerTerminated(signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        client = client_from_spec(spec)
        # A crash after dispatch but before recording the run ID is recovered by
        # adopting the deterministic run name rather than dispatching twice.
        run_state = client.find_run(task_id)
        if _cancelled(task_dir):
            # A queued runner can start after the coordinator has already
            # recorded cancellation.  Adopt and cancel an existing run, but do
            # not create a new remote side effect for a cancelled task.
            exit_code, error_type = 130, "Cancelled"
            _cancel_quietly(client, run_state, state_path)
            return exit_code
        if run_state is None:
            # Close the cancellation race as near as possible to dispatch.
            if _cancelled(task_dir):
                exit_code, error_type = 130, "Cancelled"
                return exit_code
            client.dispatch(
                task_id, str(spec.get("request_id", task_id)),
                str(spec["agent"]), str(spec["model"]),
                prompt.read_text(encoding="utf-8"),
            )
            run_state = client.wait_for_run(task_id)
        write_state(state_path, run_state, dispatched_at=utc_now())
        started = time.monotonic()
        while True:
            if _cancelled(task_dir):
                client.cancel(run_state.run_id)
                write_state(state_path, run_state, cancellation_requested_at=utc_now())
                exit_code, error_type = 130, "Cancelled"
                break
            if time.monotonic() - started > deadline_seconds:
                client.cancel(run_state.run_id)
                exit_code, error_type = 124, "WorkerTimeout"
                break
            run_state = client.status(run_state.run_id)
            write_state(state_path, run_state)
            if run_state.status == "completed":
                if run_state.conclusion != "success":
                    exit_code = 1
                    error_type = "GitHubActionsConclusion"
                    break
                files = None
                for attempt in range(12):
                    try:
                        files = client.download_artifact(run_state.run_id, artifact_name)
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
        _cancel_quietly(client, run_state, state_path, task_id)
    except (GitHubActionsError, OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        error_type = type(exc).__name__
        exit_code = 1
        _cancel_quietly(client, run_state, state_path, task_id)
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


def _cancel_quietly(client: GitHubActionsClient | None,
                    run_state: RunState | None, state_path: Path,
                    task_id: str | None = None) -> None:
    if client is None:
        return
    try:
        if run_state is None and task_id is not None:
            # SIGTERM can arrive after dispatch but before the run ID was
            # persisted.  Adopt the deterministic run and cancel it rather than
            # leaving an orphan executing in GitHub.
            run_state = client.find_run(task_id)
        if run_state is None:
            return
        client.cancel(run_state.run_id)
        write_state(state_path, run_state, cancellation_requested_at=utc_now())
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
