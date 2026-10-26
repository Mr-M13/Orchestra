"""Sanitized health and diagnostic reporting for the PixelBox coordinator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omcp.controller import Config
from omcp.platform import describe_sandbox, host_platform


def _owner_only(path: Path) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    return path.is_file() and not stat.st_mode & 0o077 and stat.st_uid == os.getuid()


def _iso_age_seconds(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        then = datetime.fromisoformat(value)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - then).total_seconds()
    # A future heartbeat must not prove liveness. Permit only small clock skew.
    return age if age >= -5 else None


def _http_check(url: str | None) -> dict[str, Any]:
    """Return checked/unchecked distinctly; a missing URL is not healthy."""
    if not url:
        return {"checked": False, "status": "unchecked"}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "orchestra-health"})
        with urllib.request.urlopen(request, timeout=5) as response:
            ok = 200 <= response.status < 300
            return {
                "checked": True, "ok": ok,
                "status": "healthy" if ok else "failed",
                "http_status": response.status,
            }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "checked": True, "ok": False, "status": "failed",
            "error": type(exc).__name__,
        }


def _tunnel_check(url: str | None) -> dict[str, Any]:
    return _http_check(url)


def _dispatch_broker_check(config: Any) -> dict[str, Any]:
    workers = [
        worker for worker in config.workers.values()
        if worker.adapter == "github_actions"
    ]
    if not workers:
        return {"checked": False, "status": "not_configured"}
    checks = []
    ok = True
    for worker in sorted(workers, key=lambda item: item.name):
        path = Path(worker.github_dispatch_socket or "")
        try:
            metadata = path.stat()
        except OSError:
            worker_ok = False
        else:
            worker_ok = bool(
                stat.S_ISSOCK(metadata.st_mode)
                and metadata.st_uid == worker.github_service_uid
                and not metadata.st_mode & 0o007
            )
        ok = ok and worker_ok
        checks.append({"worker": worker.name, "ok": worker_ok})
    return {"checked": True, "ok": ok, "workers": checks, "status": "healthy" if ok else "failed"}


def _task_health(config: Any, state: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    now = datetime.now(timezone.utc)
    recent_cutoff = now.timestamp() - 86_400
    stale_cutoff = now.timestamp() - max(300, 10 * config.heartbeat_seconds)
    tasks = state.get("tasks", {})
    failed = interrupted = startup_failures = actions_failures = stale_accepted = 0
    for task in tasks.values():
        status = task.get("status")
        failure = task.get("failure")
        failure_type = failure.get("type") if isinstance(failure, dict) else None
        finished_raw = task.get("finished_at") or task.get("created_at")
        try:
            finished = datetime.fromisoformat(finished_raw).timestamp()
        except (TypeError, ValueError):
            finished = 0
        recent = finished >= recent_cutoff
        if status == "failed" and recent:
            failed += 1
        if status == "interrupted" and recent:
            interrupted += 1
        if recent and failure_type in {
            "runner_startup_failed", "runner_disappeared", "runner_exited_without_completion",
            "startup_lease_expired", "SandboxStartupError",
        }:
            startup_failures += 1
        worker = config.workers.get(task.get("worker"))
        if (recent and worker is not None and worker.adapter == "github_actions"
                and status in {"failed", "interrupted"}):
            actions_failures += 1
        if status == "accepted":
            try:
                created = datetime.fromisoformat(task.get("created_at", "")).timestamp()
            except (TypeError, ValueError):
                created = 0
            if created <= stale_cutoff:
                stale_accepted += 1
    warnings: list[str] = []
    if failed:
        warnings.append("failed_tasks_present")
    if interrupted:
        warnings.append("interrupted_tasks_present")
    if startup_failures:
        warnings.append("runner_startup_failures")
    if actions_failures:
        warnings.append("failed_actions_dispatch")
    if stale_accepted:
        warnings.append("stale_accepted_tasks")
    return {
        "recent_failed": failed,
        "recent_interrupted": interrupted,
        "recent_startup_failures": startup_failures,
        "recent_actions_failures": actions_failures,
        "stale_accepted": stale_accepted,
        "window_seconds": 86_400,
    }, warnings


def _backup_check(config: Any, backup_dir: str | None) -> tuple[dict[str, Any], list[str]]:
    path = Path(backup_dir).expanduser() if backup_dir else config.state_dir.parent / "backup"
    if not path.is_dir():
        return {"checked": False, "status": "not_configured"}, []
    candidates: list[Path] = []
    for pattern in ("state-*.tar.gz", "state-*.tar.gz.gpg", "orchestra-state-*.tar.gz"):
        candidates.extend(path.glob(pattern))
    archives = [
        item for item in candidates
        if item.is_file() and not item.is_symlink()
    ]
    if not archives:
        # An owner may keep unrelated snapshots in this directory.  Do not
        # claim a backup is healthy or failed when no reviewed state archive
        # naming convention is present.
        return {
            "checked": False, "status": "unverified",
            "reason": "no_recognized_state_archive", "path": str(path),
        }, []
    newest = max(archives, key=lambda item: item.stat().st_mtime)
    age = max(0.0, time.time() - newest.stat().st_mtime)
    ok = age <= 7 * 86_400
    return {
        "checked": True, "ok": ok, "status": "healthy" if ok else "failed",
        "newest_age_seconds": round(age, 1), "archive_count": len(archives),
    }, [] if ok else ["backup_stale"]


def collect_health(
    config_path: Path, tunnel_url: str | None = None,
    dashboard_url: str | None = None, mcp_url: str | None = None,
    backup_dir: str | None = None,
) -> dict[str, Any]:
    """Return only safe metadata; never include instructions or credentials."""
    report: dict[str, Any] = {
        "service": "omcp", "checked_at": datetime.now(timezone.utc).isoformat(),
        "platform": host_platform(), "checks": {}, "errors": [], "warnings": [],
    }
    checks = report["checks"]
    try:
        config = Config.load(config_path)
    except Exception as exc:
        report["status"] = "failed"
        checks["configuration"] = {"ok": False, "error_type": type(exc).__name__}
        report["errors"].append("configuration")
        return report
    checks["configuration"] = {
        "ok": True,
        "owner_only": _owner_only(config_path),
        "repository_profiles": sorted(
            (config.repository_profiles or {"default": config.profile(None)})
        ),
        "workers": {
            name: {
                "adapter": worker.adapter,
                "remote": worker.adapter == "github_actions",
                "broker_socket_present": bool(
                    worker.github_dispatch_socket
                    and Path(worker.github_dispatch_socket).exists()
                ) if worker.adapter == "github_actions" else None,
                "credential_present": bool(
                    worker.credential_file and Path(worker.credential_file).exists()
                ) if worker.adapter != "github_actions" else None,
            }
            for name, worker in sorted(config.workers.items())
        },
        "execution_limit": config.max_parallel_tasks,
        "task_approval_mode": config.task_approval_mode,
        "local_worker_launcher": config.local_worker_launcher,
    }
    if not checks["configuration"]["owner_only"]:
        report["errors"].append("configuration_permissions")
    checks["sandbox"] = describe_sandbox(config.sandbox_backend)
    if not checks["sandbox"]["available"]:
        report["warnings"].append("sandbox_unavailable")

    state_path = config.state_dir / "state.json"
    try:
        state = json.loads(state_path.read_text())
        if not isinstance(state.get("tasks"), dict):
            raise ValueError
        checks["state"] = {
            "ok": True, "owner_only": _owner_only(state_path),
            "task_count": len(state["tasks"]),
        }
    except (OSError, ValueError, TypeError):
        report["status"] = "failed"
        checks["state"] = {"ok": False}
        report["errors"].append("state")
        state = {"tasks": {}}

    instance = state.get("instance") if isinstance(state, dict) else None
    state_store_id = hashlib.sha256(
        f"{config.repository.lower()}\0{config.state_dir.resolve()}".encode()
    ).hexdigest()[:24]
    checks["instance"] = {
        "instance_id": instance.get("instance_id") if isinstance(instance, dict) else None,
        "state_store_id": state_store_id,
        "instance_role": config.instance_role,
        "authoritative_instance_id": config.authoritative_instance_id,
    }

    audit_path = config.state_dir / "audit.jsonl"
    checks["audit"] = {
        "ok": _owner_only(audit_path), "present": audit_path.exists(),
        "bytes": audit_path.stat().st_size if audit_path.exists() else 0,
    }
    if not checks["audit"]["ok"]:
        report["errors"].append("audit")

    heartbeat_path = config.state_dir / "heartbeat.json"
    try:
        heartbeat = json.loads(heartbeat_path.read_text())
        age = _iso_age_seconds(heartbeat.get("last_tick"))
        pid = heartbeat.get("pid")
        alive = isinstance(pid, int) and _pid_alive(pid)
        checks["coordinator"] = {
            "ok": bool(
                alive and age is not None
                and -5 <= age <= max(30, 4 * config.heartbeat_seconds)
            ),
            "pid": pid, "age_seconds": round(age, 1) if age is not None else None,
            "active_tasks": heartbeat.get("active_tasks"),
            "execution_limit": heartbeat.get("execution_limit"),
            "last_tick": heartbeat.get("last_tick"),
        }
    except (OSError, ValueError, TypeError):
        checks["coordinator"] = {"ok": False, "reason": "heartbeat_unavailable"}
    if not checks["coordinator"]["ok"]:
        report["warnings"].append("coordinator_not_running_or_stale")

    now = time.time()
    pending = expired = 0
    for task in state.get("tasks", {}).values():
        approval = task.get("approval")
        if task.get("status") == "approval_required" and isinstance(approval, dict):
            pending += 1
            try:
                if datetime.fromisoformat(approval.get("expires_at")).timestamp() <= now:
                    expired += 1
            except (TypeError, ValueError):
                expired += 1
    checks["approvals"] = {"pending": pending, "expired": expired}
    if expired:
        report["warnings"].append("expired_approvals_present")

    active = sum(task.get("status") in {"starting", "running", "cancel_requested"}
                 for task in state.get("tasks", {}).values())
    checks["scheduler"] = {
        "active_tasks": active, "limit": config.max_parallel_tasks,
        "saturated": active >= config.max_parallel_tasks,
        "repository_parallelism": getattr(
            config, "repository_parallelism", "serialized",
        ),
        "last_repository": state.get("scheduler", {}).get("last_repository")
        if isinstance(state.get("scheduler"), dict) else None,
    }
    task_check, task_warnings = _task_health(config, state)
    checks["tasks"] = task_check
    report["warnings"].extend(task_warnings)
    checks["dispatch_broker"] = _dispatch_broker_check(config)
    if checks["dispatch_broker"].get("status") == "failed":
        report["warnings"].append("dispatch_broker_unavailable")
    checks["dashboard"] = _http_check(dashboard_url)
    checks["mcp_gateway"] = _http_check(mcp_url)
    backup, backup_warnings = _backup_check(config, backup_dir)
    checks["backup"] = backup
    report["warnings"].extend(backup_warnings)
    try:
        usage = shutil.disk_usage(config.state_dir)
        checks["disk"] = {
            "ok": usage.free >= 512 * 1024 * 1024,
            "free_bytes": usage.free, "total_bytes": usage.total,
        }
        if not checks["disk"]["ok"]:
            report["warnings"].append("low_disk")
    except OSError:
        checks["disk"] = {"ok": False}
        report["warnings"].append("disk_unavailable")

    if config.principal.identity:
        checks["authentication"] = {
            "tunnel_asserted": os.environ.get("OMCP_TUNNEL_AUTHENTICATED") == "1",
            "caller_matches": os.environ.get("OMCP_AUTHENTICATED_CALLER")
            == config.principal.identity,
        }
    checks["tunnel"] = _tunnel_check(tunnel_url)

    if report["errors"]:
        report["status"] = "failed"
    elif report["warnings"]:
        report["status"] = "degraded"
    else:
        report["status"] = "ok"
    return report


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report sanitized Orchestra health")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tunnel-health-url")
    parser.add_argument("--dashboard-health-url")
    parser.add_argument("--mcp-health-url")
    parser.add_argument("--backup-dir")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-degraded", action="store_true")
    args = parser.parse_args(argv)
    report = collect_health(
        args.config, args.tunnel_health_url, args.dashboard_health_url,
        args.mcp_health_url, args.backup_dir,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"status: {report['status']}")
        print(f"platform: {report['platform']}")
        for name, value in report["checks"].items():
            print(f"{name}: {json.dumps(value, sort_keys=True)}")
        for error in report["errors"]:
            print(f"error: {error}")
        for warning in report["warnings"]:
            print(f"warning: {warning}")
    if report["status"] == "failed":
        return 1
    if report["status"] == "degraded" and not args.allow_degraded:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
