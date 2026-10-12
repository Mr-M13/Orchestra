"""Durable, at-most-one macOS approval notification per approval request."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class NotificationSpec:
    notification_id: str
    task_id: str
    title: str
    repository: str
    worker: str
    body: str
    portal_base_url: str
    portal_token_path: str


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    temporary.replace(path)
    os.chmod(path, 0o600)


def _approval_key(task: dict[str, Any]) -> str:
    approval = task.get("approval", {})
    raw = f"{task.get('task_id', '')}\0{approval.get('requested_at', '')}".encode()
    return hashlib.sha256(raw).hexdigest()


class ApprovalNotificationAgent:
    """Project pending approvals into native notifications with durable deduplication."""

    def __init__(
        self,
        state_dir: Path,
        notifier_executable: Path,
        portal_base_url: str,
        portal_token_path: Path,
        runner: Callable[[list[str]], None] | None = None,
        remover: Callable[[str], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.state_dir = state_dir.resolve()
        self.coordinator_state_path = self.state_dir / "state.json"
        self.ledger_path = self.state_dir / "approval-notifications.json"
        self.spec_dir = self.state_dir / "approval-notification-specs"
        self.notifier_executable = notifier_executable.resolve()
        self.portal_base_url = portal_base_url.rstrip("/")
        self.portal_token_path = portal_token_path.resolve()
        self.runner = runner or self._run_notifier
        self.remover = remover or self._remove_notification
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self.spec_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.spec_dir, 0o700)

    def _load_ledger(self) -> dict[str, Any]:
        try:
            value = json.loads(self.ledger_path.read_text())
        except FileNotFoundError:
            value = {"schema_version": 1, "approvals": {}}
        if value.get("schema_version") != 1 or not isinstance(value.get("approvals"), dict):
            raise RuntimeError("invalid approval notification ledger")
        return value

    def _run_notifier(self, command: list[str]) -> None:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        if result.returncode:
            raise RuntimeError(f"notification helper failed with exit {result.returncode}")

    def _remove_notification(self, notification_id: str) -> None:
        self._run_notifier([
            str(self.notifier_executable), "--remove", notification_id,
        ])

    def _spec(self, task: dict[str, Any], notification_id: str) -> NotificationSpec:
        repository = str(task.get("repository", "unknown repository"))
        worker = str(task.get("worker", "unknown worker"))
        task_id = str(task["task_id"])
        return NotificationSpec(
            notification_id=notification_id,
            task_id=task_id,
            title=f"Orchestra approval: {task.get('title', task_id)}",
            repository=repository,
            worker=worker,
            body=f"{repository} · {worker} · {task_id}",
            portal_base_url=self.portal_base_url,
            portal_token_path=str(self.portal_token_path),
        )

    def scan_once(self) -> dict[str, int]:
        coordinator = json.loads(self.coordinator_state_path.read_text())
        tasks = coordinator.get("tasks")
        if not isinstance(tasks, dict):
            raise RuntimeError("invalid coordinator task state")
        ledger = self._load_ledger()
        approvals = ledger["approvals"]
        current_time = self.now()
        counts = {"sent": 0, "failed": 0, "closed": 0, "unchanged": 0}

        active_keys: set[str] = set()
        for task in tasks.values():
            approval = task.get("approval")
            if not isinstance(approval, dict) or not approval.get("requested_at"):
                continue
            key = _approval_key(task)
            active_keys.add(key)
            notification_id = "omcp.approval." + key[:32]
            record = approvals.setdefault(key, {
                "task_id": task.get("task_id"),
                "requested_at": approval.get("requested_at"),
                "notification_id": notification_id,
                "delivery_status": "pending",
                "attempts": 0,
            })
            is_pending = task.get("status") == "approval_required" and approval.get("status") == "pending"
            expires_at = approval.get("expires_at")
            try:
                expired = isinstance(expires_at, str) and datetime.fromisoformat(expires_at) <= current_time
            except ValueError:
                expired = True
            if not isinstance(expires_at, str):
                expired = True
            if expired:
                is_pending = False
            if not is_pending:
                if record.get("delivery_status") != "closed":
                    try:
                        self.remover(notification_id)
                    except Exception as exc:
                        record["remove_error_type"] = type(exc).__name__
                    record.update({
                        "delivery_status": "closed",
                        "terminal_status": "expired" if expired else task.get("status"),
                        "closed_at": current_time.isoformat(),
                    })
                    counts["closed"] += 1
                else:
                    counts["unchanged"] += 1
                continue
            if record.get("delivery_status") == "sent":
                counts["unchanged"] += 1
                continue
            next_attempt = record.get("next_attempt_at")
            if isinstance(next_attempt, str) and datetime.fromisoformat(next_attempt) > current_time:
                counts["unchanged"] += 1
                continue

            spec = self._spec(task, notification_id)
            spec_path = self.spec_dir / f"{key}.json"
            _atomic_json(spec_path, asdict(spec))
            record.update({
                "delivery_status": "delivering",
                "attempts": int(record.get("attempts", 0)) + 1,
                "last_attempt_at": current_time.isoformat(),
            })
            _atomic_json(self.ledger_path, ledger)
            try:
                self.runner([str(self.notifier_executable), "--notify", str(spec_path)])
            except Exception as exc:
                delay = min(60, 2 ** min(int(record["attempts"]), 5))
                record.update({
                    "delivery_status": "failed",
                    "last_error_type": type(exc).__name__,
                    "next_attempt_at": (current_time + timedelta(seconds=delay)).isoformat(),
                })
                counts["failed"] += 1
            else:
                record.update({
                    "delivery_status": "sent",
                    "sent_at": current_time.isoformat(),
                })
                record.pop("last_error_type", None)
                record.pop("next_attempt_at", None)
                counts["sent"] += 1

        for key, record in approvals.items():
            if key not in active_keys and record.get("delivery_status") != "closed":
                record.update({
                    "delivery_status": "closed", "terminal_status": "removed",
                    "closed_at": current_time.isoformat(),
                })
                counts["closed"] += 1
        _atomic_json(self.ledger_path, ledger)
        return counts

    def run_forever(self, poll_seconds: float) -> None:
        while True:
            try:
                self.scan_once()
            except Exception as exc:
                # The durable task remains authoritative; retry after the poll interval.
                print(f"approval notification scan failed ({type(exc).__name__})", file=sys.stderr, flush=True)
            time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deliver durable OMCP approval notifications")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--notifier-executable", type=Path, required=True)
    parser.add_argument("--portal-base-url", required=True)
    parser.add_argument("--portal-token-path", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 0.5 <= args.poll_seconds <= 60:
        raise ValueError("poll-seconds must be between 0.5 and 60")
    agent = ApprovalNotificationAgent(
        args.state_dir, args.notifier_executable, args.portal_base_url,
        args.portal_token_path,
    )
    if args.once:
        agent.scan_once()
        return 0
    agent.run_forever(args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
