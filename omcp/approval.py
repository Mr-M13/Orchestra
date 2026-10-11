"""Local, interactive human approval for tasks submitted through OMCP."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from omcp.controller import Config, TaskController


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approve or reject an OMCP task locally")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--decision", choices=("approve", "reject"), required=True)
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise PermissionError("human approval requires an interactive local terminal")
    reason = input("Human approval reason: ").strip()
    if not reason:
        raise ValueError("a non-empty approval reason is required")
    controller = TaskController(Config.load(args.config))
    try:
        result = controller.resolve_approval(
            args.task_id, args.decision, reason,
            approver_identity="local:" + getpass.getuser(),
        )
        print(f"{result['task_id']}: {result['status']}")
        return 0
    finally:
        controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
