"""Bounded, deduplicated operational alerts with an external dead-man ping."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from omcp.health import collect_health


SCHEMA_VERSION = 1
DEFAULT_CONFIG = Path("/etc/orchestra/alerts.json")
DEFAULT_STATE = Path("/home/hamed/orchestra-migration/state/alert-state.json")
MAX_PAYLOAD_BYTES = 16 * 1024


class AlertError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _age_seconds(value: object, now: datetime) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (now - moment).total_seconds())


def _private(path: Path) -> bool:
    try:
        metadata = path.stat()
    except OSError:
        return False
    return path.is_file() and not path.is_symlink() and not metadata.st_mode & 0o077 and os.access(path, os.R_OK)


@dataclass(frozen=True)
class AlertConfig:
    path: Path
    healthcheck_ping_url: str | None
    webhook_url: str | None
    state_file: Path
    threshold_seconds: int
    dedup_seconds: int
    disk_free_bytes: int

    @classmethod
    def load(cls, path: Path) -> "AlertConfig":
        path = Path(path)
        if not path.exists():
            raise AlertError("alert configuration does not exist")
        if not _private(path):
            raise AlertError("alert configuration must be owner-only")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AlertError("alert configuration is invalid") from exc
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise AlertError("unsupported alert configuration schema")
        ping = value.get("healthcheck_ping_url")
        webhook = value.get("webhook_url")
        for label, candidate in (("healthcheck", ping), ("webhook", webhook)):
            if candidate is not None and (not isinstance(candidate, str) or not candidate.startswith(("https://", "http://"))):
                raise AlertError(f"{label} URL is invalid")
        threshold = value.get("threshold_seconds", 600)
        dedup = value.get("dedup_seconds", 3600)
        disk = value.get("disk_free_bytes", 512 * 1024 * 1024)
        for number in (threshold, dedup, disk):
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise AlertError("alert thresholds must be positive integers")
        return cls(
            path=path, healthcheck_ping_url=ping, webhook_url=webhook,
            state_file=Path(value.get("state_file", DEFAULT_STATE)),
            threshold_seconds=threshold, dedup_seconds=dedup,
            disk_free_bytes=disk,
        )


def _atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
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


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "schema_version": SCHEMA_VERSION, "baseline_done": False,
            "active": {}, "seen_failures": [], "seen_interruptions": [],
            "last_run": None,
        }
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return {
            "schema_version": SCHEMA_VERSION, "baseline_done": False,
            "active": {}, "seen_failures": [], "last_run": None,
        }
    value.setdefault("active", {})
    value.setdefault("seen_failures", [])
    value.setdefault("seen_interruptions", [])
    value.setdefault("baseline_done", False)
    return value


def _http(method: str, url: str, payload: dict[str, Any] | None, timeout: int = 8) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": "orchestra-alerts", "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise AlertError("alert endpoint returned an error")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AlertError("alert endpoint is unavailable") from exc


def alert_health(config_path: Path | None, *, now: float | None = None) -> dict[str, Any]:
    """Report whether an external alert channel has run recently."""
    if config_path is None or not Path(config_path).exists():
        return {
            "checked": False, "status": "unchecked",
            "reason": "alert_config_not_configured",
            "next_action": "Configure an external alert channel.",
        }
    try:
        config = AlertConfig.load(Path(config_path))
    except AlertError as exc:
        return {
            "checked": True, "status": "failed", "reason": "invalid_alert_config",
            "error_type": type(exc).__name__,
            "operator_action": "Repair the owner-only alert configuration.",
        }
    if not config.healthcheck_ping_url and not config.webhook_url:
        return {
            "checked": False, "status": "unchecked",
            "reason": "alert_channel_not_configured",
            "next_action": "Configure healthchecks/webhook delivery.",
        }
    try:
        state = json.loads(config.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "checked": False, "status": "unchecked",
            "reason": "no_alert_run",
            "next_action": "Run the alert service and verify firing/recovery.",
        }
    last_run = state.get("last_run") if isinstance(state, dict) else None
    age = _age_seconds(last_run, _now())
    if age is None:
        return {
            "checked": True, "status": "failed", "reason": "invalid_alert_state",
            "operator_action": "Repair or remove the alert state file.",
        }
    if age > 900:
        return {
            "checked": True, "status": "failed", "reason": "alert_run_overdue",
            "last_observed": last_run,
            "operator_action": "Check the alert timer/service and external channel.",
        }
    return {
        "checked": True, "status": "healthy", "last_observed": last_run,
        "active": sorted(state.get("active", {})) if isinstance(state.get("active"), dict) else [],
    }


def _deliver(config: AlertConfig, firing: list[dict[str, Any]], recovering: list[dict[str, Any]],
             active: dict[str, Any], sender: Callable[[str, str, dict[str, Any] | None], None] | None = None) -> None:
    sender = sender or _http
    for event in firing:
        if config.webhook_url:
            sender("POST", config.webhook_url, {
                "event": event["event"], "status": "firing",
                "component": event["component"], "reason": event["reason"],
                "first_observed": event["first_observed"],
                "last_observed": event["last_observed"],
                "operator_action": event["operator_action"],
            })
    for event in recovering:
        if config.webhook_url:
            sender("POST", config.webhook_url, {
                "event": event["event"], "status": "recovered",
                "component": event["component"], "reason": event["reason"],
                "last_observed": event["last_observed"],
            })
    if config.healthcheck_ping_url:
        url = config.healthcheck_ping_url.rstrip("/")
        if active:
            sender("GET", url + "/fail", None)
        else:
            sender("GET", url, None)


def _new_event(key: str, component: str, reason: str, now: datetime,
               state: dict[str, Any], operator_action: str) -> dict[str, Any]:
    previous = state["active"].get(key)
    first = previous.get("first_observed") if isinstance(previous, dict) else None
    return {
        "key": key, "event": key, "component": component, "reason": reason,
        "first_observed": first or _iso(now), "last_observed": _iso(now),
        "operator_action": operator_action,
    }


def evaluate(config: AlertConfig, health: dict[str, Any], state: dict[str, Any],
             *, now: datetime | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Turn a health report into deduplicated firing/recovery transitions."""
    now = now or _now()
    firing: list[dict[str, Any]] = []
    current: dict[str, dict[str, Any]] = {}
    tasks = health.get("checks", {}).get("tasks", {})
    readiness = health.get("checks", {}).get("readiness", {})
    checks = health.get("checks", {})
    if checks.get("coordinator", {}).get("ok") is False:
        current["coordinator"] = _new_event(
            "coordinator", "coordinator", "not running or heartbeat stale", now, state,
            "Start the coordinator and check the service logs.",
        )
    if readiness.get("status") == "unavailable":
        current["readiness"] = _new_event(
            "readiness", "orchestra", "no operational coding worker", now, state,
            "Restore a verified coding lane; inspect the worker last attempt.",
        )
    for name in ("dashboard", "mcp_gateway", "tunnel"):
        check = checks.get(name, {})
        if isinstance(check, dict) and check.get("checked") and check.get("status") == "failed":
            current[name] = _new_event(
                name, name, "health probe failed", now, state,
                f"Check {name} service and its loopback/tunnel endpoint.",
            )
    backup = checks.get("backup", {})
    if isinstance(backup, dict) and backup.get("status") == "failed":
        current["backup"] = _new_event(
            "backup", "backup", str(backup.get("reason") or "backup failed"), now, state,
            "Run a backup and inspect restic/status; verify restore after repair.",
        )
    disk = checks.get("disk", {})
    if isinstance(disk, dict) and disk.get("ok") is False:
        current["disk"] = _new_event(
            "disk", "host", "disk free space below threshold", now, state,
            "Free space or expand storage before continuing.",
        )
    queue = readiness.get("queue", {}) if isinstance(readiness.get("queue"), dict) else {}
    if queue.get("stalled"):
        current["queue_stalled"] = _new_event(
            "queue_stalled", "scheduler", "accepted queue has stopped advancing", now, state,
            "Inspect accepted/running tasks and coordinator scheduling.",
        )
    if isinstance(tasks, dict):
        for task_id in tasks.get("recent_failed_ids", []):
            if not isinstance(task_id, str):
                continue
            key = f"task_failure:{task_id}"
            seen = state.setdefault("seen_failures", [])
            if key in state.get("active", {}):
                current[key] = {
                    **state["active"][key], "last_observed": _iso(now),
                }
            elif task_id not in seen:
                seen.append(task_id)
                state["seen_failures"] = seen[-1000:]
                current[key] = _new_event(
                    key, "tasks", "new failed task",
                    now, state, "Inspect the failed task diagnostics and result.",
                )
        for task_id in tasks.get("recent_interrupted_ids", []):
            if not isinstance(task_id, str):
                continue
            key = f"task_interrupted:{task_id}"
            seen = state.setdefault("seen_interruptions", [])
            if key in state.get("active", {}):
                current[key] = {
                    **state["active"][key], "last_observed": _iso(now),
                }
            elif task_id not in seen:
                seen.append(task_id)
                state["seen_interruptions"] = seen[-1000:]
                current[key] = _new_event(
                    key, "tasks", "new interrupted task", now, state,
                    "Inspect the interrupted task and runner state.",
                )
        if tasks.get("stale_accepted", 0) > 0 or tasks.get("recent_startup_failures", 0) > 0:
            current["stuck_tasks"] = _new_event(
                "stuck_tasks", "scheduler", "stuck accepted or startup-failed task(s)",
                now, state, "Inspect accepted tasks and startup logs.",
            )
    previous = state.get("active", {})
    for key, event in current.items():
        if key not in previous:
            firing.append(event)
    recovering = [
        {**value, "event": key, "status": "recovered"}
        for key, value in previous.items() if key not in current
    ]
    state["active"] = current
    return firing, recovering, state


def run(config_path: Path, health_config: Path, *, backup_config: Path | None = None,
        tunnel_url: str | None = None, dashboard_url: str | None = None,
        mcp_url: str | None = None, sender: Callable[[str, str, dict[str, Any] | None], None] | None = None) -> dict[str, Any]:
    config = AlertConfig.load(config_path)
    if not config.webhook_url and not config.healthcheck_ping_url:
        return {
            "status": "unchecked", "reason": "alert_channel_not_configured",
            "firing": 0, "recovering": 0,
        }
    health = collect_health(
        health_config, tunnel_url, dashboard_url, mcp_url,
        backup_config=str(backup_config) if backup_config else None,
    )
    state = _load_state(config.state_file)
    baseline = not state.get("baseline_done")
    firing, recovering, state = evaluate(config, health, state)
    if baseline:
        # Do not page for the historical task database on first start. Current
        # component failures still alert immediately.
        historical = {
            "recent_task_failures", "interrupted_tasks", "stuck_tasks",
            "task_failure", "task_interrupted",
        }
        def is_historical(key: str) -> bool:
            return any(key == item or key.startswith(item + ":") for item in historical)
        firing = [item for item in firing if not is_historical(item["key"])]
        state["active"] = {
            key: value for key, value in state["active"].items()
            if not is_historical(key)
        }
        state["baseline_done"] = True
    _deliver(config, firing, recovering, state["active"], sender)
    state["last_run"] = _iso()
    _atomic(config.state_file, state)
    return {
        "status": "baseline" if baseline else "delivered",
        "firing": len(firing), "recovering": len(recovering),
        "active": sorted(state["active"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate Orchestra operational alerts")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--health-config", type=Path, required=True)
    parser.add_argument("--backup-config", type=Path)
    parser.add_argument("--tunnel-health-url")
    parser.add_argument("--dashboard-health-url")
    parser.add_argument("--mcp-health-url")
    args = parser.parse_args(argv)
    try:
        result = run(
            args.config, args.health_config, backup_config=args.backup_config,
            tunnel_url=args.tunnel_health_url,
            dashboard_url=args.dashboard_health_url,
            mcp_url=args.mcp_health_url,
        )
    except AlertError as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
