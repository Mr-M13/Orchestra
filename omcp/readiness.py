"""Current Orchestra readiness, separate from historical task history."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

ACTIVE = {"starting", "running", "cancel_requested"}
TERMINAL = {"succeeded", "failed", "cancelled", "rejected", "interrupted"}
CODING_ADAPTERS = {"codex", "opencode", "github_actions"}


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


def worker_health_from_state(config: Any, state: dict[str, Any]) -> Callable[[str], dict[str, Any]]:
    """Derive the last verified result for each configured worker."""
    tasks = state.get("tasks", {})

    def lookup(name: str) -> dict[str, Any]:
        attempts = [
            task for task in tasks.values()
            if task.get("worker") == name and task.get("status") in TERMINAL
        ]
        if not attempts:
            return {
                "proven_healthy": False,
                "health_reason": "no_terminal_attempt",
                "last_attempt": None,
            }
        last = max(
            attempts,
            key=lambda task: (
                task.get("finished_at") or task.get("created_at") or "",
                task.get("task_id") or "",
            ),
        )
        failure = last.get("failure") if isinstance(last.get("failure"), dict) else None
        category = failure.get("category") if failure else None
        adapter = config.workers[name].adapter
        proven = adapter in CODING_ADAPTERS and last.get("status") == "succeeded"
        if adapter == "deepseek":
            reason = "response_only"
        elif proven:
            reason = "verified_success"
        elif category == "credit_exhausted":
            reason = "last_attempt_credit_exhausted"
        elif last.get("status") == "cancelled":
            reason = "last_attempt_cancelled"
        elif last.get("status") in {"failed", "interrupted"}:
            reason = "last_attempt_failed"
        else:
            reason = f"last_attempt_{last.get('status')}"
        return {
            "proven_healthy": proven,
            "health_reason": reason,
            "last_attempt": {
                key: last.get(key) for key in (
                    "task_id", "status", "exit_code", "finished_at",
                )
            } | ({"failure_category": category} if category else {}),
        }

    return lookup


def build_readiness(
    config: Any, state: dict[str, Any], *,
    worker_health: Callable[[str], dict[str, Any]] | None = None,
    coordinator_ok: bool = True,
    now: datetime | None = None,
    queue_stall_seconds: int = 600,
) -> dict[str, Any]:
    """Return whether supported tasks can be accepted and completed now."""
    now = now or datetime.now(timezone.utc)
    tasks = state.get("tasks", {})
    active = [task for task in tasks.values() if task.get("status") in ACTIVE]
    accepted = sorted(
        (task for task in tasks.values() if task.get("status") == "accepted"),
        key=lambda task: (task.get("created_at", ""), task.get("task_id", "")),
    )
    active_by_worker = {task.get("worker"): task for task in active}
    capacity = len(active) < config.max_parallel_tasks
    workers: list[dict[str, Any]] = []
    coding_operational: list[str] = []
    idle_coding: list[str] = []
    for name in sorted(config.workers):
        worker = config.workers[name]
        health = (
            worker_health(name) if worker_health is not None
            else {"proven_healthy": False, "health_reason": "unverified", "last_attempt": None}
        )
        current = active_by_worker.get(name)
        if worker.adapter == "deepseek":
            role = "response_only"
            operational = bool(
                health.get("last_attempt")
                and health["last_attempt"].get("status") == "succeeded"
            )
        elif worker.adapter == "github_actions":
            role = "remote_unproven" if not health.get("proven_healthy") else "remote_coding"
            operational = bool(health.get("proven_healthy"))
        else:
            role = "coding"
            operational = bool(health.get("proven_healthy"))
        if role in {"coding", "remote_coding"} and operational:
            coding_operational.append(name)
            if current is None:
                idle_coding.append(name)
        workers.append({
            "worker": name, "adapter": worker.adapter, "role": role,
            "operational": operational,
            "available_now": current is None and capacity and operational,
            "current_task_id": current.get("task_id") if current else None,
            "health_reason": health.get("health_reason"),
            "last_attempt": health.get("last_attempt"),
        })

    oldest_age = _age_seconds(accepted[0].get("created_at"), now) if accepted else None
    queue_stalled = bool(
        oldest_age is not None and oldest_age >= queue_stall_seconds
        and len(active) < config.max_parallel_tasks
    )
    reasons: list[str] = []
    if not coordinator_ok:
        reasons.append("coordinator_unavailable")
    if not coding_operational:
        reasons.append("no_operational_coding_worker")
    if capacity is False:
        reasons.append("execution_capacity_saturated")
    if not idle_coding and capacity:
        reasons.append("operational_workers_busy")
    if queue_stalled:
        reasons.append("queue_not_advancing")

    if not coordinator_ok or not coding_operational:
        status = "unavailable"
    elif capacity is False or not idle_coding or queue_stalled:
        status = "degraded"
    else:
        status = "ready"
    return {
        "as_of": now.isoformat(),
        "status": status,
        "accepting": coordinator_ok,
        "can_complete_now": status == "ready",
        "reasons": reasons,
        "workers": workers,
        "queue": {
            "active": len(active), "limit": config.max_parallel_tasks,
            "accepted": len(accepted),
            "oldest_accepted_seconds": round(oldest_age, 1) if oldest_age is not None else None,
            "stalled": queue_stalled,
        },
    }
