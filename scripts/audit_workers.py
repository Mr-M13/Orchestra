#!/usr/bin/env python3
"""Credit-free audit of configured Orchestra worker capabilities.

This command never submits a task, opens a provider connection, reads a
credential value, or infers funding from ``available: true``.  It reports what
the owner configuration and local filesystem can prove, and marks the rest as
unverified so an operator can schedule a separate, authorized acceptance test.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omcp.controller import Config, Worker, failure_category  # noqa: E402
from omcp.platform import host_platform  # noqa: E402


def _owner_only_file(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"configured": False}
    try:
        metadata = path.stat()
    except OSError:
        return {"configured": True, "present": False}
    return {
        "configured": True, "present": True,
        "regular": path.is_file(), "owner_only": not metadata.st_mode & 0o077
        and metadata.st_uid == os.getuid(),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _directory_metadata(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"configured": False}
    try:
        metadata = path.stat()
    except OSError:
        return {"configured": True, "present": False}
    return {
        "configured": True, "present": True, "directory": path.is_dir(),
        "owner_only": not metadata.st_mode & 0o077 and metadata.st_uid == os.getuid(),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _executable_metadata(worker: Worker) -> dict[str, Any]:
    if not worker.command:
        return {"configured": False, "reason": "no local executable"}
    executable = Path(worker.command[0])
    result: dict[str, Any] = {
        "configured": True, "path": str(executable),
        "present": executable.is_file() and os.access(executable, os.X_OK),
    }
    if not result["present"]:
        return result
    try:
        completed = subprocess.run(
            [str(executable), "--version"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            check=False, timeout=5, env={"PATH": os.environ.get("PATH", "")},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result["version"] = {"checked": True, "ok": False, "error_type": type(exc).__name__}
        return result
    text = completed.stdout.decode("utf-8", "replace").strip().splitlines()
    result["version"] = {
        "checked": True,
        "ok": completed.returncode == 0,
        # The version probe is local and credit-free; retain only one bounded line.
        "text": text[0][:200] if text else "",
        "exit_code": completed.returncode,
    }
    return result


def _socket_metadata(path: str | None, expected_uid: int | None) -> dict[str, Any]:
    if not path:
        return {"configured": False}
    socket = Path(path)
    try:
        metadata = socket.stat()
    except OSError:
        return {"configured": True, "present": False}
    return {
        "configured": True, "present": True,
        "socket": stat.S_ISSOCK(metadata.st_mode),
        "owner_matches": metadata.st_uid == expected_uid,
        "world_accessible": bool(metadata.st_mode & 0o007),
    }


def _memory_report(limit: int) -> dict[str, Any]:
    total: int | None = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError):
        try:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (OSError, ValueError):
            total = None
    result: dict[str, Any] = {
        "observed_total_bytes": total,
        "configured_parallel_tasks": limit,
        "measurement_required": True,
        "recommendation": (
            "Measure two real coding workers, Nextcloud and existing services "
            "before setting MemoryMax/CPUQuota. Do not choose a limit that can "
            "trigger an unreported OOM kill."
        ),
    }
    if total is not None:
        reserve = 1024 * 1024 * 1024
        result["planning_available_bytes_after_1g_reserve"] = max(0, total - reserve)
        result["planning_per_worker_bytes"] = max(0, total - reserve) // limit
    return result


def _workflow_check(config: Config, dispatch_config: Path | None) -> dict[str, Any]:
    if dispatch_config is None:
        return {"checked": False, "reason": "dispatch_config_not_supplied"}
    try:
        raw = json.loads(dispatch_config.read_text())
    except (OSError, ValueError):
        return {"checked": True, "ok": False, "reason": "dispatch_config_unreadable"}
    if not isinstance(raw, dict):
        return {"checked": True, "ok": False, "reason": "dispatch_config_invalid"}
    workflow_sha = raw.get("workflow_sha")
    workflow_ref = raw.get("workflow_ref")
    result: dict[str, Any] = {
        "checked": True, "workflow_sha": workflow_sha,
        "workflow_ref": workflow_ref,
    }
    if not isinstance(workflow_sha, str) or not isinstance(workflow_ref, str):
        result.update({"ok": False, "reason": "workflow_identity_missing"})
        return result
    try:
        completed = subprocess.run(
            ["git", "-C", str(config.project_root), "rev-parse",
             f"origin/{workflow_ref}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=False, timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        result.update({"ok": False, "reason": "git_unavailable"})
        return result
    current = completed.stdout.decode("utf-8", "replace").strip()
    result["protected_ref_sha"] = current or None
    result["ok"] = completed.returncode == 0 and current == workflow_sha
    if not result["ok"]:
        result["reason"] = "workflow_sha_differs_from_local_protected_ref"
    return result


def _state_summary(config: Config) -> dict[str, Any]:
    try:
        state = json.loads((config.state_dir / "state.json").read_text())
    except (OSError, ValueError):
        return {"tasks": {}, "active": 0}
    tasks = state.get("tasks") if isinstance(state, dict) else None
    if not isinstance(tasks, dict):
        return {"tasks": {}, "active": 0}
    active = sum(
        task.get("status") in {"starting", "running", "cancel_requested"}
        for task in tasks.values()
    )
    return {"tasks": tasks, "active": active}


def _workspace_changes(config: Config, workspace: object) -> bool | None:
    if not isinstance(workspace, str):
        return None
    path = Path(workspace)
    try:
        if not path.resolve().is_relative_to(config.state_dir.resolve()) or not path.is_dir():
            return None
        completed = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=normal"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=False, timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return bool(completed.stdout.strip())


def build_audit(config: Config, dispatch_config: Path | None = None) -> dict[str, Any]:
    workers: list[dict[str, Any]] = []
    warnings: list[str] = []
    state = _state_summary(config)
    capacity = state["active"] < config.max_parallel_tasks
    for name in sorted(config.workers):
        worker = config.workers[name]
        edits = worker.adapter in {"codex", "opencode", "github_actions"}
        worker_tasks = [
            task for task in state["tasks"].values() if task.get("worker") == name
        ]
        active_task = next(
            (task for task in worker_tasks
             if task.get("status") in {"starting", "running", "cancel_requested"}),
            None,
        )
        terminal = sorted(
            (task for task in worker_tasks
             if task.get("status") in {"succeeded", "failed", "interrupted", "cancelled", "rejected"}),
            key=lambda task: (
                task.get("finished_at") or task.get("created_at") or "",
                task.get("task_id") or "",
            ),
            reverse=True,
        )
        last = terminal[0] if terminal else None
        changes = _workspace_changes(config, last.get("workspace")) if last else None
        status = last.get("status") if last else None
        failure = last.get("failure") if last else None
        failure_type = failure.get("type") if isinstance(failure, dict) else None
        category = failure_category(last) if last and status != "succeeded" else None
        proven = bool(
            edits and last is not None and status == "succeeded"
            and changes is True and failure_type is None
        )
        if not last:
            health_reason = "no_terminal_task"
        elif worker.adapter == "deepseek":
            health_reason = "response_only"
        elif category == "credit_exhausted":
            health_reason = "last_task_credit_exhausted"
        elif category == "malformed_completion":
            health_reason = "last_task_malformed_completion"
        elif category == "contract_path_denied":
            health_reason = "last_task_contract_path_denied"
        elif status != "succeeded":
            health_reason = f"last_task_{status}"
        elif changes is not True:
            health_reason = "no_verified_workspace_change"
        else:
            health_reason = "verified_coding_result"
        next_action = None
        if category == "credit_exhausted":
            next_action = (
                f"Add credits to {name}'s workspace, then run a separately "
                "authorised small verification task."
            )
        elif category == "contract_path_denied":
            next_action = (
                "Repair the shared routine policy so the task-local "
                ".orchestra-contract path is writable; the task did not run."
            )
        entry: dict[str, Any] = {
            "worker": name,
            "adapter": worker.adapter,
            "configured": True,
            "idle": active_task is None,
            "capacity_available": capacity,
            "runnable": active_task is None and capacity,
            "proven_healthy": proven,
            "health_reason": health_reason,
            "failure_category": category,
            "next_action": next_action,
            "edits_repository": edits,
            "response_only": worker.adapter == "deepseek",
            "endpoint_host": worker.endpoint_host,
            "credential": _owner_only_file(
                Path(worker.credential_file) if worker.credential_file else None
            ),
            "funding": "unverified",
            "credits": "unverified",
            "last_task": None if last is None else {
                "task_id": last.get("task_id"),
                "status": status,
                "failure_type": failure_type,
                "failure_category": category,
                "exit_code": last.get("exit_code"),
                "finished_at": last.get("finished_at"),
                "workspace_changes": changes,
            },
        }
        if worker.adapter == "github_actions":
            entry.update({
                "agent": worker.agent,
                "model": worker.model,
                "dispatch_socket": _socket_metadata(
                    worker.github_dispatch_socket, worker.github_service_uid,
                ),
            })
            if not entry["dispatch_socket"].get("present"):
                warnings.append(f"{name}: dispatch broker socket is unavailable")
        else:
            entry.update({
                "executable": _executable_metadata(worker),
                "codex_home": _directory_metadata(
                    Path(worker.codex_home).expanduser()
                    if worker.codex_home else None
                ),
            })
        if worker.adapter == "deepseek":
            warnings.append(
                f"{name}: response-only adapter; do not use it as a coding writer"
            )
        warnings.append(f"{name}: provider funding/credits are not verified by this audit")
        workers.append(entry)
    workflow = _workflow_check(config, dispatch_config)
    if workflow.get("checked") and not workflow.get("ok"):
        warnings.append("dispatch broker workflow_sha needs review")
    return {
        "service": "orchestra-worker-audit",
        "platform": host_platform(),
        "repository": config.repository,
        "repository_parallelism": getattr(
            config, "repository_parallelism", "serialized",
        ),
        "workers": workers,
        "dispatch_workflow": workflow,
        "memory": _memory_report(config.max_parallel_tasks),
        "warnings": warnings,
        "note": (
            "available:true and a configured credential do not prove funding or "
            "successful execution. A paid acceptance test requires separate owner "
            "authorization."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit Orchestra workers without credits")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dispatch-config", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config)
    except Exception as exc:
        print(f"configuration error: {type(exc).__name__}", file=sys.stderr)
        return 1
    report = build_audit(config, args.dispatch_config)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"repository: {report['repository']}")
        print(f"parallelism: {report['repository_parallelism']}")
        for worker in report["workers"]:
            kind = "response-only" if worker["response_only"] else "coding"
            print(f"- {worker['worker']}: {worker['adapter']} ({kind})")
            print(
                f"  configured={worker['configured']} idle={worker['idle']} "
                f"runnable={worker['runnable']} proven_healthy={worker['proven_healthy']} "
                f"reason={worker['health_reason']} "
                f"failure={worker.get('failure_category')}"
            )
            if worker.get("next_action"):
                print(f"  next_action: {worker['next_action']}")
            print(f"  funding: {worker['funding']} (not inferred from availability)")
            if "executable" in worker:
                version = worker["executable"].get("version", {})
                print(f"  executable: {worker['executable'].get('present')} "
                      f"version={version.get('text') or 'unchecked'}")
            if "dispatch_socket" in worker:
                print(f"  dispatch socket: {worker['dispatch_socket']}")
        workflow = report["dispatch_workflow"]
        print(f"dispatch workflow: {workflow}")
        memory = report["memory"]
        print("memory: "
              f"total={memory.get('observed_total_bytes')} "
              f"planning_per_worker={memory.get('planning_per_worker_bytes')}")
        for warning in report["warnings"]:
            print(f"warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
