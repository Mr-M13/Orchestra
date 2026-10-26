"""Read-only, privacy-filtered MCP view of the local Mac Bridge state."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SERVER_NAME = "orchestra-bridge-status"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSIONS = {"2025-06-18", "2025-11-25"}
MAX_STATE_BYTES = 2_000_000
DEFAULT_STATE_FILE = Path("/Users/hamed/Library/Application Support/Orchestra/state/state.json")
SAFE_WORKERS = {"codex", "dzn-ina", "dzn-deepseek"}
SAFE_STATUSES = {
    "queued",
    "claimed",
    "running",
    "succeeded",
    "failed",
    "interrupted",
    "blocked",
}


def _state_file() -> Path:
    # This environment value is fixed by the local stdio launcher, not supplied
    # by MCP callers. The server exposes no path-selection argument or tool.
    return Path(os.environ.get("ORCHESTRA_STATE_FILE", str(DEFAULT_STATE_FILE)))


def bridge_summary() -> dict[str, Any]:
    """Return aggregate health only; never expose task IDs, paths, PIDs, or logs."""
    path = _state_file()
    if path.stat().st_size > MAX_STATE_BYTES:
        raise RuntimeError("Orchestra state file exceeds the configured size limit")
    with path.open("r", encoding="utf-8") as stream:
        state = json.load(stream)
    if not isinstance(state, dict):
        raise RuntimeError("Orchestra state has an invalid shape")

    tasks = state.get("tasks", {})
    workers = state.get("workers", {})
    if not isinstance(tasks, dict) or not isinstance(workers, dict):
        raise RuntimeError("Orchestra state has an invalid shape")

    task_statuses: Counter[str] = Counter()
    worker_statuses: dict[str, Counter[str]] = {}
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        status = task.get("status")
        if isinstance(status, str) and status in SAFE_STATUSES:
            task_statuses[status] += 1
            worker = task.get("worker")
            if isinstance(worker, str) and worker in SAFE_WORKERS:
                worker_statuses.setdefault(worker, Counter())[status] += 1

    active_workers: dict[str, bool] = {}
    for name, value in workers.items():
        if name in SAFE_WORKERS and isinstance(value, dict):
            active_workers[name] = value.get("running") is True

    heartbeat = state.get("heartbeat")
    heartbeat_age_seconds = None
    if isinstance(heartbeat, str):
        try:
            seen = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            heartbeat_age_seconds = max(
                0, int((datetime.now(timezone.utc) - seen).total_seconds())
            )
        except ValueError:
            pass

    return {
        "bridge": "Mac Bridge",
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "task_counts_by_status": dict(sorted(task_statuses.items())),
        "worker_running": dict(sorted(active_workers.items())),
        "worker_task_counts_by_status": {
            worker: dict(sorted(counts.items()))
            for worker, counts in sorted(worker_statuses.items())
        },
        "authority": "read-only summary; approvals and execution remain in the existing Bridge",
    }


def _tool_result() -> dict[str, Any]:
    summary = bridge_summary()
    return {
        "content": [{"type": "text", "text": json.dumps(summary, sort_keys=True)}],
        "structuredContent": summary,
        "isError": False,
    }


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized" or method == "notifications/cancelled":
        return None
    if method == "initialize":
        params = message.get("params") or {}
        requested = params.get("protocolVersion")
        if requested not in PROTOCOL_VERSIONS:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "Unsupported protocol version"},
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": "Read-only aggregate Bridge status. No approvals or execution controls are exposed.",
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "bridge_status",
                        "description": "Read aggregate health and task counts from the local Orchestra Mac Bridge. Does not expose task IDs, prompts, logs, paths, or process IDs, and cannot approve or execute work.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
                    }
                ]
            },
        }
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != "bridge_status" or params.get("arguments", {}) not in ({}, None):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "Unknown tool"},
            }
        try:
            return {"jsonrpc": "2.0", "id": request_id, "result": _tool_result()}
        except Exception:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "Bridge status unavailable; inspect the local Bridge directly."}],
                    "isError": True,
                },
            }
    if request_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        }
    return None


def main() -> None:
    for line in sys.stdin:
        try:
            if len(line) > 65_536:
                continue
            message = json.loads(line)
            if not isinstance(message, dict):
                continue
            response = _handle(message)
        except Exception:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Invalid JSON-RPC message"},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
