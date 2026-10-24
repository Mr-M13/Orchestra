"""Fixed, credential-free subprocess used by the Phase 3B adapter tests."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def run(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if set(spec) != {
        "version", "attempt_id", "task_id", "proposal_digest", "instructions",
        "workspace", "result_path", "behavior", "delay_seconds", "max_output_bytes",
    } or spec["version"] != 1:
        raise ValueError("invalid disposable harness specification")
    workspace = Path(spec["workspace"])
    result_path = Path(spec["result_path"])
    if result_path.parent != spec_path.parent or workspace.parent != spec_path.parent:
        raise ValueError("disposable paths escaped the adapter job directory")
    instructions = spec["instructions"]
    maximum = spec["max_output_bytes"]
    if not isinstance(instructions, str) or not isinstance(maximum, int):
        raise ValueError("invalid disposable task bounds")
    if len(instructions.encode("utf-8")) > maximum:
        raise ValueError("disposable task exceeded its bound")

    started = _now()
    cancelled = False

    def terminate(_signum: int, _frame: Any) -> None:
        nonlocal cancelled
        cancelled = True
        raise InterruptedError

    signal.signal(signal.SIGTERM, terminate)
    try:
        behavior = spec["behavior"]
        if behavior == "interrupt":
            os._exit(75)
        if behavior == "sleep":
            time.sleep(float(spec["delay_seconds"]))
        if behavior == "fail":
            raise RuntimeError("requested disposable failure")
        target = workspace / "src" / "result.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = ("Disposable coding result\n" + instructions + "\n").encode("utf-8")
        if len(payload) > maximum:
            payload = payload[:maximum]
        target.write_bytes(payload)
        _atomic_json(result_path, {
            "version": 1, "outcome": "succeeded", "started_at": started,
            "finished_at": _now(), "artifact": "src/result.txt",
            "artifact_sha256": hashlib.sha256(payload).hexdigest(),
            "artifact_size": len(payload),
        })
        return 0
    except InterruptedError:
        _atomic_json(result_path, {"version": 1, "outcome": "cancelled",
            "started_at": started, "finished_at": _now(), "artifact": None})
        return 130
    except Exception:
        _atomic_json(result_path, {"version": 1, "outcome": "failed",
            "started_at": started, "finished_at": _now(), "artifact": None})
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True, type=Path)
    return run(parser.parse_args(argv).spec)


if __name__ == "__main__":
    raise SystemExit(main())
