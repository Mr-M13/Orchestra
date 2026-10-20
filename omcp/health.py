"""Sanitized health and diagnostic reporting for the PixelBox coordinator."""

from __future__ import annotations

import argparse
import json
import os
import shutil
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


def _tunnel_check(url: str | None) -> dict[str, Any]:
    if not url:
        return {"checked": False}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "orchestra-health"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return {
                "checked": True, "ok": 200 <= response.status < 300,
                "status": response.status,
            }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"checked": True, "ok": False, "error": type(exc).__name__}


def collect_health(config_path: Path, tunnel_url: str | None = None) -> dict[str, Any]:
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
                "credential_present": bool(
                    worker.credential_file and Path(worker.credential_file).exists()
                ),
            }
            for name, worker in sorted(config.workers.items())
        },
        "execution_limit": config.max_parallel_tasks,
        "task_approval_mode": config.task_approval_mode,
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
        "last_repository": state.get("scheduler", {}).get("last_repository")
        if isinstance(state.get("scheduler"), dict) else None,
    }
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
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-degraded", action="store_true")
    args = parser.parse_args(argv)
    report = collect_health(args.config, args.tunnel_health_url)
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
